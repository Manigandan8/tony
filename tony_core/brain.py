# =====================================================================
# tony_core/brain.py
# Brain orchestration, provider chain (9 brains), tool loop, fast path.
#
# SHARED MUTABLE GLOBALS REFERENCED AND MUTATED ACROSS MODULES:
# - zai.ZAI_BRAIN_MODEL (rebound on successful call)
# - zai.ZAI_BRAIN_MODELS (items removed on error)
# - cohereai.COHERE_BRAIN_MODELS (items removed on error)
# - github_models.GITHUB_BRAIN_MODELS (items removed on error)
# - mistralai.MISTRAL_BRAIN_MODELS (items removed on error)
# - cerebras.CEREBRAS_BRAIN_MODELS (items removed on error)
# - safety.TURN (turn actions and failure tracking)
# - speech.STOP (stop request flag)
# =====================================================================

import re
import json
import time
from google.genai import types as gemini_types

from tony_core import config
from tony_core.net import HTTP
from tony_core import overlay
from tony_core import speech
from tony_core import safety
from tony_core import memory
from tony_core.tools import TOOLS, run_tool, _wrap
from tony_core.tools import ui
from tony_core.ears import _mic_command
from tony_core.providers import (
    groqai, gemini, zai, cohereai, openrouter, sambanova,
    github_models, mistralai, cerebras
)


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
    fg = _live_foreground_title()
    return config.SYSTEM + (
        f"\nCURRENT MACHINE STATE (live): Foreground window = '{fg}'; "
        f"Chrome running = {ui.STATE['chrome_running']}; "
        f"active profile = {ui.STATE['chrome_profile']}; last site = {ui.STATE['last_site']}.\n"
        f"STRICT ROUTING RULES FOR VIDEOS & CLICKS:\n"
        f"- When the user says 'in the screen', 'on the screen', 'click', or is already on YouTube and asks to play a visible video, "
        f"call `click_screen(element=...)` ONLY ONCE and NEVER call `open_on_site` afterward (do not open a new tab!).\n"
        f"- Only call `open_on_site(site='youtube', what=...)` when the user wants to search/play a video NOT currently on screen."
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
    T("calculator_math", "Do ANY math safely: '3137*3371', '15% of 840', 'sqrt(144)'.", {"expression": {"type": "string"}}, ["expression"]),
    T("open_folder", "Open a folder in File Explorer INSTANTLY.", {"folder": {"type": "string"}}, ["folder"]),
    T("create_folder", "Create a folder (any depth), VERIFIED on disk.", {"path": {"type": "string"}}, ["path"]),
    T("delete_folder", "Delete an EMPTY folder (safe).", {"path": {"type": "string"}}, ["path"]),
    T("write_file", "Create or overwrite a file.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    T("append_to_file", "Append a chunk to a file.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    T("read_file", "Read a text/PDF file.", {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
    T("list_files", "List/search files newest first.", {"folder": {"type": "string"}, "pattern": {"type": "string"}}, []),
    T("browser_search", "Open a VISIBLE Google search tab in Chrome.", {"query": {"type": "string"}}, ["query"]),
    T("open_on_site", "Open and play the EXACT video or page on a site (YouTube, GitHub, Spotify, etc.).", {"site": {"type": "string"}, "what": {"type": "string"}}, ["site", "what"]),
    T("open_app", "Open a DESKTOP application by name (not websites — use open_website for those).", {"app": {"type": "string"}}, ["app"]),
    T("open_website", "Open a website HOMEPAGE in the browser (use for 'open youtube', 'open instagram', etc.).", {"url": {"type": "string"}, "browser": {"type": "string", "enum": ["chrome", "brave", "edge"]}}, ["url"]),
    T("open_chrome_profile", "Switch Chrome to a named profile.", {"profile": {"type": "string"}}, ["profile"]),
    T("restore_tabs", "Restore Chrome's last session.", {}, []),
    T("press_hotkey", "Keyboard shortcut: 'ctrl+t', 'f5', 'alt+tab', YouTube keys (k/l/j/f/m).", {"keys": {"type": "string"}, "app": {"type": "string"}}, ["keys"]),
    T("type_text", "Type or paste text into the FOCUSED window.", {"text": {"type": "string"}, "app": {"type": "string"}}, ["text"]),
    T("ui_list", "List clickable elements BY NAME in an open app.", {"app": {"type": "string"}}, ["app"]),
    T("ui_click", "Click a named control in a native desktop app via UI Automation.", {"app": {"type": "string"}, "element_name": {"type": "string"}}, ["app", "element_name"]),
    T("click_screen", "Set-of-Mark + Vision + UIA screen clicker with verification: clicks any visible video, thumbnail, link, or button on screen (including 'first video', 'second video', or specific video titles).", {"app": {"type": "string"}, "element": {"type": "string"}}, ["element"]),
    T("close_window", "Close an app's windows (chrome, edge, camera, webcam, notepad, etc.).", {"what": {"type": "string"}}, ["what"]),
    T("scroll_screen", "Scroll the mouse wheel on the FOCUSED window.", {"direction": {"type": "string", "enum": ["up", "down"]}, "seconds": {"type": "number"}, "speed": {"type": "string", "enum": ["slow", "normal", "fast"]}}, ["direction"]),
    T("look_at_screen", "Screenshot + describe when asked about the screen.", {"question": {"type": "string"}}, ["question"]),
    T("verify_screen", "Screenshot + check expectation when asked.", {"expectation": {"type": "string"}}, ["expectation"]),
    T("run_command", "Run a Windows shell or PowerShell command.", {"command": {"type": "string"}}, ["command"]),
    T("set_volume", "Set speaker volume 0-100.", {"level": {"type": "integer"}}, ["level"]),
    T("media_control", "Media keys: play, pause, next, previous, mute.", {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "mute"]}}, ["action"]),
    T("set_reminder", "Set a spoken reminder after N minutes.", {"minutes": {"type": "number"}, "text": {"type": "string"}}, ["minutes", "text"]),
    T("take_note", "Save a timestamped note.", {"text": {"type": "string"}}, ["text"]),
    T("weather", "Current weather via city name.", {"city": {"type": "string"}}, []),
    T("system_status", "RAM, battery, Chrome state.", {}, []),
    T("run_routine", "Run a saved routine from routines.json.", {"name": {"type": "string"}}, ["name"]),
    T("remember_fact", "Store a lasting fact.", {"fact": {"type": "string"}}, ["fact"]),
    T("read_clipboard", "Read text currently copied in the Windows clipboard.", {}, []),
    T("write_clipboard", "Copy text directly into the Windows clipboard.", {"text": {"type": "string"}}, ["text"]),
    T("fetch_webpage", "Extract full readable text from any webpage URL.", {"url": {"type": "string"}}, ["url"]),
    T("deep_research", "Multi-source internet research on a topic and optionally save a report file.", {"topic": {"type": "string"}, "save_to_file": {"type": "string"}}, ["topic"]),
    T("top_processes", "List top 8 Windows processes by memory or CPU usage.", {"sort_by": {"type": "string", "enum": ["memory", "cpu"]}}, []),
    T("active_window_info", "Get the title of the currently focused foreground window.", {}, []),
    T("system_power", "Lock the workstation ('lock'), put PC to sleep ('sleep'), 'shutdown', or 'restart'.", {"action": {"type": "string", "enum": ["lock", "sleep", "shutdown", "restart"]}}, ["action"]),
    T("get_secret", "Read a secret API key or variable from .env (privileged mode only).", {"name": {"type": "string"}}, ["name"]),
]


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
    safety.TURN["actions"] = []
    safety.TURN["failures"] = []
    for _ in range(8):
        if speech._stop_requested():
            speech.STOP["now"] = False
            return "Stopped by your request, sir."
        m = _normalize_assistant_msg(call_fn(messages))
        tcs = m.get("tool_calls")
        if not tcs:
            final = (m.get("content") or "").strip()
            break
        messages.append(m)
        for tc in tcs:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            isfile = fn_name in ("write_file", "append_to_file")
            print(f"  [tool: {fn_name} {args if not isfile else {'path': args.get('path'), 'content_len': len(args.get('content', ''))}}]")
            out = run_tool(fn_name, args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(out)[:1500]})
    return final


def _run_brain_with_nudge(call_fn, msg: str) -> str:
    speech.STOP["now"] = False
    overlay.ui_state("THINKING")
    with memory.MEMORY_LOCK:
        hist_snapshot = list(memory.HISTORY)
    messages = [{"role": "system", "content": system_prompt()}] + hist_snapshot + [{"role": "user", "content": msg}]
    final = _tool_loop(call_fn, messages)
    nudge = (
        "A tool FAILED but you claimed success. Read the tool output and tell the user the TRUTH "
        "in one short sentence. If a folder/file/element wasn't found, say exactly that — and retry ONCE with a corrected call if useful."
    )
    for attempt in range(2):
        claimed = bool(re.search(r"\b(opened|created|deleted|set|started|done|wrote|completed|running|clicked)\b", (final or ""), re.I))
        had_fail = any(("Do NOT claim success" in f) or ("No folder" in f) or ("No such file" in f) for f in safety.TURN.get("failures", []))
        noop = (final is None or final == "" or (not safety.TURN["actions"] and final.lower().strip(" .!") in ("done", "ok", "okay")))
        if not (noop or (had_fail and claimed)):
            break
        print(f"  (failure-over-claim or no-op — nudging brain, round {attempt+1})")
        messages.append({"role": "user", "content": nudge})
        final = _tool_loop(call_fn, messages)
    final = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", (final or "Done.")).strip()
    if safety.TURN["actions"]:
        final += f" [Done this turn: {'; '.join(safety.TURN['actions'])}]"
    memory.remember("user", msg)
    memory.remember("assistant", final)
    return final


def _groq_call(messages):
    last = None
    for _ in range(2):
        try:
            resp = groqai.groq.chat.completions.create(
                model=groqai.BRAIN_MODEL,
                messages=messages,
                tools=GROQ_TOOLS,
                temperature=0.2,
                max_tokens=4000,
            )
            return resp.choices[0].message
        except Exception as e:
            last = e
            print("  (groq hiccup — retrying)")
            time.sleep(1)
    raise last


def brain_groq(msg: str) -> str:
    return _run_brain_with_nudge(_groq_call, msg)


def brain_gemini(msg: str):
    if not gemini.client:
        return None
    with memory.MEMORY_LOCK:
        history = [{"role": "model" if t["role"] == "assistant" else "user", "parts": [{"text": t["content"]}]} for t in memory.HISTORY]
    safety.TURN["actions"] = []
    safety.TURN["failures"] = []
    for _ in range(2):
        try:
            chat = gemini.client.chats.create(
                model=gemini.GEMINI_BRAIN_MODEL,
                config=gemini_types.GenerateContentConfig(
                    system_instruction=system_prompt(),
                    tools=[_wrap(f) for f in TOOLS.values()],
                ),
                history=history,
            )
            answer = (chat.send_message(msg).text or "Done.").strip()
            if safety.TURN["actions"]:
                answer += f" [Done this turn: {'; '.join(safety.TURN['actions'])}]"
            memory.remember("user", msg)
            memory.remember("assistant", answer)
            return answer
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                return None
            time.sleep(1)
    return None


def brain_zai(msg: str):
    if not zai.ZAI_API_KEY or not zai.ZAI_BRAIN_MODELS:
        return None
    base = zai._zai_base()
    for zmodel in list(zai.ZAI_BRAIN_MODELS):
        def _call(messages, model_id=zmodel, use_thinking=True):
            payload = {
                "model": model_id,
                "messages": messages,
                "tools": GROQ_TOOLS,
                "temperature": 0.2,
                "max_tokens": 4000,
            }
            if use_thinking:
                payload["thinking"] = {"type": "disabled"}
            r = HTTP.post(f"{base}/chat/completions", headers=zai._zai_headers(), json=payload, timeout=60)
            if r.status_code == 400 and use_thinking:
                return _call(messages, model_id, use_thinking=False)
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
                print(f"  [zai/{zmodel} unavailable on this key — trying next]")
                continue
            print(f"  [zai brain failed: {code[:100]}]")
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
                json={"model": model_id, "messages": messages, "tools": GROQ_TOOLS, "temperature": 0.2, "max_tokens": 4000},
                timeout=45,
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
                print(f"  [cohere/{cm} unavailable on this key — trying next]")
                continue
            print(f"  [cohere brain failed: {code[:80]}]")
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
                json={"model": model_id, "messages": messages, "tools": GROQ_TOOLS, "temperature": 0.2, "max_tokens": 4000},
                timeout=45,
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
            json={"model": "gpt-oss-120b", "messages": messages, "tools": GROQ_TOOLS, "temperature": 0.2, "max_tokens": 4000},
            timeout=45,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()["choices"][0]["message"]
    try:
        return _run_brain_with_nudge(_call, msg)
    except Exception:
        return None


def _generic_openai_brain(msg, base, key, models, tag, timeout=45):
    """Shared loop for GitHub Models / Mistral / Cerebras (all OpenAI-compatible)."""
    if not key or not models:
        return None
    for m in list(models):
        def _call(messages, model_id=m):
            r = HTTP.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model_id, "messages": messages, "tools": GROQ_TOOLS, "temperature": 0.2, "max_tokens": 4000},
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
                print(f"  [{tag}: key rejected (401) — check the token in .env]")
                return None
            if "HTTP 404" in code or "HTTP 400" in code:
                if m in models:
                    models.remove(m)
                print(f"  [{tag}/{m} unavailable on this key — trying next]")
                continue
            print(f"  [{tag} brain failed: {code[:80]}]")
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
    chain = ((brain_zai, brain_groq) if config.ZAI_AS_PRIMARY else (brain_groq, brain_zai)) + (
        brain_github, brain_mistral, brain_cerebras, brain_gemini, brain_cohere, brain_or, brain_sambanova
    )
    tags = {brain_groq: f"groq/{groqai.BRAIN_MODEL}", brain_zai: f"zai/{zai.ZAI_BRAIN_MODEL}"}
    first_ok = True
    for fn in chain:
        try:
            ans = fn(msg)
            if ans:
                if first_ok:
                    first_ok = False
                    print(f"  [brain: {tags.get(fn, fn.__name__.replace('brain_', ''))}, {time.time()-t0:.1f}s]")
                return ans
        except Exception as e:
            print(f"  [{fn.__name__.replace('brain_', '')} failed: {str(e)[:60]} — falling back]")
    return "All of my brains are exhausted right now. Try again in a bit."


def fast_path(msg: str):
    _r = _mic_command(msg)
    if _r is not None:
        return _r

    # 1) Direct screen click fast-path:
    # Matches "click [on] the second video", "now play the Dola video in the screen", etc.
    m = re.match(
        r"^\s*(?:now\s+)?(?:please\s+)?(?:click|select|tap|play|open)(?:\s+on)?\s+(?:the\s+)?(.{2,60}?)\s+(?:in|on)\s+(?:the\s+)?screen\s*[.!]?\s*$",
        msg, re.I
    )
    if not m:
        m = re.match(
            r"^\s*(?:now\s+)?(?:please\s+)?(?:click|select|tap)(?:\s+on)?\s+(?:the\s+)?(.{2,60}?)\s*[.!]?\s*$",
            msg, re.I
        )
    if m:
        elem = m.group(1).strip()
        if not re.search(r"\b(?:and|then|after)\b", elem, re.I):
            return memory.core_reply(run_tool("click_screen", {"element": elem}))

    # 2) Direct YouTube play fast-path: "play <what> on youtube"
    m = re.match(r"^\s*(?:now\s+)?(?:please\s+)?(?:play|watch)\s+(.{2,70}?)\s+on\s+youtube\s*[.!]?\s*$", msg, re.I)
    if m:
        return memory.core_reply(run_tool("open_on_site", {"site": "youtube", "what": m.group(1).strip()}))

    # 3) Open app / website fast-path
    m = re.match(r"^\s*(?:please\s+)?open\s+(?:the\s+|my\s+)?([\w .\-]{2,20})\s*[.!]?\s*$", msg, re.I)
    if m and m.group(1).strip().lower() not in ("the", "my", "folder"):
        target = m.group(1).strip()
        if "folder" not in target.lower():
            site = config.WEBSITE_ALIASES.get(target.lower())
            if site and target.lower() not in config.APPS:
                return memory.core_reply(run_tool("open_website", {"url": site}))
            return memory.core_reply(run_tool("open_app", {"app": target}))

    # 4) Close window fast-path
    m = re.match(r"^\s*(?:please\s+)?close\s+(?:the\s+|my\s+)?([\w .\-]{2,20})\s*[.!]?\s*$", msg, re.I)
    if m and m.group(1).strip().lower() not in ("the", "my"):
        return memory.core_reply(run_tool("close_window", {"what": m.group(1).strip()}))

    # 5) Volume & Media fast-paths
    m = re.match(r"^\s*(?:please\s+)?(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\s*%?\s*[.!]?\s*$", msg, re.I)
    if m:
        return memory.core_reply(run_tool("set_volume", {"level": int(m.group(1))}))
    if re.match(r"^\s*(?:please\s+)?mute\s*[.!]?\s*$", msg, re.I):
        return memory.core_reply(run_tool("media_control", {"action": "mute"}))
    if re.match(r"^\s*(?:please\s+)?(?:pause|pause\s+video|pause\s+music|resume|play\s+video)\s*[.!]?\s*$", msg, re.I):
        act = "pause" if "pause" in msg.lower() else "play"
        return memory.core_reply(run_tool("media_control", {"action": act}))

    # 6) Scroll fast-path
    m = re.match(r"^\s*(?:please\s+)?scroll\s+(up|down)(?:\s+(slow|fast))?\s*[.!]?\s*$", msg, re.I)
    if m:
        return memory.core_reply(run_tool("scroll_screen", {"direction": m.group(1).lower(), "speed": (m.group(2) or "normal").lower()}))

    # 7) Folder & Routine fast-paths
    m = re.match(r"^\s*(?:please\s+)?open\s+(?:the\s+|my\s+)?([\w\-/ ]{1,30}?)\s+folder(?:\s+on\s+(?:my\s+)?desktop)?\s*[.!]?\s*$", msg, re.I)
    if m:
        name = m.group(1).strip().replace("/", "\\").replace("desktop\\", "").replace("Desktop\\", "")
        return memory.core_reply(run_tool("open_folder", {"folder": f"Desktop\\{name}" if "\\" in name else name}))
    m = re.match(r"^\s*(?:please\s+)?open\s+(?:my\s+)?(desktop|downloads|documents|pictures|music|videos|tony)\s+folder\s*[.!]?\s*$", msg, re.I)
    if m:
        return memory.core_reply(run_tool("open_folder", {"folder": m.group(1)}))
    m = re.match(r"^\s*(?:please\s+)?(?:run|start|do)\s+(?:the\s+)?([\w\- ]{1,20}?)\s+routine\s*[.!]?\s*$", msg, re.I)
    if m:
        return memory.core_reply(run_tool("run_routine", {"name": m.group(1).strip()}))
    return None