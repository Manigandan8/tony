import os, subprocess, asyncio, time, difflib, json, glob, re, io, base64, warnings
warnings.filterwarnings("ignore")
from dotenv import load_dotenv
from tavily import TavilyClient
from google import genai
from google.genai import types
from groq import Groq
import edge_tts, pygame

load_dotenv()
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=20 * 1000)
)
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
groq = Groq(api_key=os.environ["GROQ_API_KEY"])

VOICE_ENGINE = "offline"
APPS = {"chrome": "start chrome", "edge": "start msedge", "brave": "start brave",
        "notepad": "start notepad", "calculator": "start calc", "spotify": "start spotify:",
        "task manager": "start taskmgr", "explorer": "start explorer"}
EXES = {"edge": "msedge.exe", "notepad": "notepad.exe", "calculator": "ApplicationFrameHost.exe",
        "spotify": "spotify.exe", "brave": "brave.exe", "task manager": "taskmgr.exe"}
CHROME_FLAG = '--force-renderer-accessibility'   # exposes WEB content to the UIA tree

# ---------- MODEL DISCOVERY (404-proof) ----------
def _groq_model_set():
    try:
        resp = groq.models.list()
        data = getattr(resp, "data", resp)
        available = set()
        for m in data:
            if isinstance(m, str):
                available.add(m)
            elif isinstance(m, (tuple, list)):
                available.add(m[0])
            else:
                mid = getattr(m, "id", None)
                if mid:
                    available.add(mid)
        return available
    except Exception:
        return set()

AVAILABLE_MODELS = _groq_model_set()

def pick_vision_model():
    hints = ("vision", "scout", "maverick", "llama-4")
    for m in sorted(AVAILABLE_MODELS):
        if any(h in m.lower() for h in hints):
            if not any(x in m.lower() for x in ("tts", "guard", "embed")):
                return m
    return None

GROQ_VISION_MODEL = pick_vision_model()
print(f"[vision] using: {GROQ_VISION_MODEL or 'none (UIA primary, Gemini fallback)'}")

def pick_groq_model():
    prefs = ["gpt-oss-120b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile",
             "llama-3.1-8b-instant", "openai/gpt-oss-20b", "gemma2-9b-it"]
    for p in prefs:
        if p in AVAILABLE_MODELS:
            return p
    for a in sorted(AVAILABLE_MODELS):
        if not any(x in a.lower() for x in ("whisper", "tts", "guard", "embed", "playai", "scout", "maverick", "vision")):
            return a
    return prefs[0]

BRAIN_MODEL = pick_groq_model()
print(f"[brain] using: {BRAIN_MODEL}\n")

# ---------- MEMORY ----------
MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tony_memory.json")
HISTORY = []
if os.path.exists(MEMORY_FILE):
    try:
        with open(MEMORY_FILE) as f:
            HISTORY = json.load(f)[-12:]
        print(f"[memory] restored {len(HISTORY)} messages from your last session")
    except Exception:
        HISTORY = []

def remember(role, content):
    HISTORY.append({"role": role, "content": content})
    with open(MEMORY_FILE, "w") as f:
        json.dump(HISTORY[-20:], f)

# ---------- LIVE MACHINE STATE ----------
STATE = {"chrome_running": False, "chrome_profile": "default", "last_site": "none"}

def _chrome_running():
    out = subprocess.run('tasklist /FI "IMAGENAME eq chrome.exe" /NH', shell=True,
                         capture_output=True, text=True).stdout.lower()
    return "chrome.exe" in out

STATE["chrome_running"] = _chrome_running()

def _dir_for(profile_name):
    user_data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    try:
        with open(os.path.join(user_data, "Local State"), encoding="utf-8") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
        for dirname, meta in info.items():
            if (meta.get("name") or "").lower() == profile_name.lower():
                return dirname
    except Exception:
        pass
    return "Default"

def _launch_chrome(url=""):
    cmd = f'start chrome {CHROME_FLAG}'
    if STATE["chrome_profile"] != "default":
        cmd += f' --profile-directory="{_dir_for(STATE["chrome_profile"])}"'
    if url:
        cmd += f' "{url}"'
    subprocess.run(cmd, shell=True)
    STATE["chrome_running"] = True

def _close_chrome_gently(timeout=8):
    if not _chrome_running():
        return False
    subprocess.run('taskkill /IM chrome.exe', shell=True, capture_output=True)  # no /F = graceful
    for _ in range(int(timeout * 2)):
        if not _chrome_running():
            return True
        time.sleep(0.5)
    subprocess.run('taskkill /IM chrome.exe /F', shell=True, capture_output=True)
    time.sleep(1.0)
    return True

def _focus_window(title_hint: str) -> bool:
    try:
        import pyautogui
        wins = pyautogui.getWindowsWithTitle(title_hint)
        if wins:
            try:
                wins[0].restore()
            except Exception:
                pass
            wins[0].activate()
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return False

def _wait_for_window_ready(title_hint: str, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if _focus_window(title_hint):
            time.sleep(1.5)
            return True
        time.sleep(0.7)
    return False
# ---------- THE BRAIN'S RULES ----------
SYSTEM = """You are Tony, a desktop assistant like Jarvis on the user's Windows PC.
RULES:
- HONESTY: Only claim what tool outputs confirm. NEVER invent Chrome flags (--restore-last-session does not exist).
- UNIVERSAL CONTROL: ui_click is your TIER-1 clicker — works in ANY app (chrome, spotify, calculator, word, anything) by element NAME. Use ui_list first when unsure of exact names. click_screen is TIER-2 (vision) for apps UIA can't see.
- CONTEXT FIRST: Read history AND the live MACHINE STATE. Resolve "that", "it", "in that tab", "login with my account" from what you DID ([Done this turn: ...] notes) and the state. Chain requests; never make the user repeat.
- TABS: Chrome is closed GENTLY (session saved); tabs should restore on relaunch. If not, restore_tabs (Ctrl+Shift+T) or click a 'Restore' bubble.
- GO TO A SITE AND FIND SOMETHING: "open poki and search subway surfers", "play sapphire song on youtube" -> open_on_site(site, what). Opens the EXACT page directly. YouTube timestamps become &t= URLs.
- BROWSER CHOICE: user names a browser -> open_website(url, browser='brave').
- SCROLLING: scroll_screen(direction, seconds, speed) on the FOCUSED window.
- KEYBOARD: press_hotkey('ctrl+t'/'f5'/'ctrl+w', app='...'). TYPE: type_text('hello', app='...') into the focused window.
- PLAIN "search X" (no site) -> browser_search. "what is X" -> answer verbally via web_search. Homepage -> open_website.
- WINDOW STATE: "only that tab" -> close_window (whole windows only).
- PROFILE LOGIC: "login with my X account" = open_chrome_profile('X'). Never ask which website unless no account named. LOGINS: never type passwords.
- SCREEN: look_at_screen / verify_screen ONLY when the user explicitly asks about the screen or something looks wrong. NEVER check automatically.
- NEVER reply with a bare "Done." and NEVER invent "[Done this turn: ...]" yourself — either call the right tool or give a real spoken answer (max 2 short spoken sentences, no markdown).
- You have ONLY these tools: web_search, browser_search, open_on_site, open_website, open_app, open_chrome_profile, close_window, restore_tabs, press_hotkey, type_text, ui_list, ui_click, click_screen, scroll_screen, look_at_screen, verify_screen, run_command. Never claim abilities you don't have.
"""

def system_prompt():
    return SYSTEM + (
        f"\nCURRENT MACHINE STATE (live — trust this over guesses): "
        f"Chrome running = {STATE['chrome_running']}; "
        f"active Chrome profile = {STATE['chrome_profile']}; "
        f"last website opened = {STATE['last_site']}."
    )

# ---------- MOUTH ----------
_offline_engine = None
def speak(text: str):
    if not text:
        return
    t0 = time.time()
    global _offline_engine
    if VOICE_ENGINE == "offline":
        import pyttsx3
        if _offline_engine is None:
            _offline_engine = pyttsx3.init()
            _offline_engine.setProperty("rate", 175)
        _offline_engine.say(text)
        _offline_engine.runAndWait()
    else:
        async def make():
            await edge_tts.Communicate(text, "en-US-GuyNeural").save("tony_tts.mp3")
        asyncio.run(make())
        pygame.mixer.init()
        pygame.mixer.music.load("tony_tts.mp3")
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.music.unload()
    print(f"  [mouth: {time.time()-t0:.1f}s]")   

# ---------- UI AUTOMATION: UNIVERSAL (any Windows app) ----------
CLICKABLE_TYPES = ("Button", "Hyperlink", "TabItem", "MenuItem", "ListItem",
                   "CheckBox", "RadioButton", "ComboBox", "DataItem")

def _uia_window(app_hint=""):
    """Find a top-level window by loose name match across ALL running apps."""
    try:
        from pywinauto import Desktop
    except ImportError:
        return None
    d = Desktop(backend="uia")
    hint = (app_hint or "").lower().strip()
    try:
        for w in d.windows():
            title = (w.window_text() or "").lower()
            if hint and hint in title:
                try:
                    w.set_focus()
                    time.sleep(0.4)
                except Exception:
                    pass
                return w
    except Exception:
        pass
    return None

def _uia_candidates(win):
    cands = []
    try:
        for el in win.descendants():
            try:
                ct = el.element_info.control_type
                if ct in CLICKABLE_TYPES:
                    name = (el.window_text() or "").strip()
                    if name:
                        cands.append((el, name, ct))
            except Exception:
                continue
    except Exception:
        pass
    return cands

def ui_list(app: str = "") -> str:
    """List every clickable element BY NAME in any open app's window (chrome, spotify, notepad, word, ANY app). Use before ui_click when unsure of exact names."""
    win = _uia_window(app)
    if not win:
        return f"Couldn't find a window like '{app}'. Is it open and not minimized?"
    cands = _uia_candidates(win)
    if not cands:
        return ("No named clickable elements exposed yet — app may still be loading, or it's a "
                "custom-drawn app. Use click_screen (vision) instead.")
    seen, out = set(), []
    for _, name, ct in cands:
        key = f"{ct}: {name}"
        if key not in seen and len(out) < 80:
            seen.add(key)
            out.append(key)
    return "Clickable elements in this window:\n" + "\n".join(out)

def ui_click(app: str, element_name: str) -> str:
    """TIER-1 CLICKER (universal): find a control BY NAME in ANY app's UIA tree and click it. Instant, offline, precise. If not found, fall back to click_screen (vision)."""
    win = _uia_window(app)
    if not win:
        return (f"Couldn't find a window like '{app}'. Use open_app first, or fall back to "
                f"click_screen. Do NOT claim success.")
    cands = _uia_candidates(win)
    if not cands:
        return (f"No UIA elements exposed by '{app}' — fall back to click_screen (vision) for this app.")
    want = element_name.lower().strip()
    exact = [(el, n, ct) for el, n, ct in cands if n.lower() == want]
    if not exact:
        partial = [(el, n, ct) for el, n, ct in cands if want in n.lower() or n.lower() in want]
        exact = partial if partial else []
    if not exact:
        names = [n.lower() for _, n, _ in cands]
        close = difflib.get_close_matches(want, names, n=1, cutoff=0.5)
        if close:
            exact = [(el, n, ct) for el, n, ct in cands if n.lower() == close[0]]
    if not exact:
        sample = ", ".join(n for _, n, _ in cands[:12])
        return (f"'{element_name}' not found in {app}'s UI elements. Closest available names: "
                f"{sample}... Use ui_list to see all, or click_screen (vision). Do NOT claim success.")
    el, n, ct = exact[0]
    try:
        el.click_input()
        time.sleep(0.8)
        return (f"Clicked {ct} '{n}' in {app} via UI Automation (precise, no vision needed). "
                f"Ask the user to confirm the result.")
    except Exception:
        try:
            el.invoke()
            time.sleep(0.8)
            return f"Invoked {ct} '{n}' in {app} via UIA. Ask the user to confirm the result."
        except Exception as e:
            return f"Found '{n}' but couldn't click it ({str(e)[:60]}) — fall back to click_screen."

# ---------- VISION (fallback tier) ----------
def _screenshot_image():
    from PIL import ImageGrab
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return img, buf.getvalue()

def _screenshot_text(instruction: str) -> str:
    _, raw = _screenshot_image()
    if GROQ_VISION_MODEL:
        b64 = base64.b64encode(raw).decode()
        try:
            r = groq.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
                max_tokens=200)
            out = (r.choices[0].message.content or "").strip()
            if out:
                return out
        except Exception as e:
            print(f"  [groq vision unavailable: {str(e)[:60]}]")
    try:
        r = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[types.Part.from_bytes(data=raw, mime_type="image/png"), instruction])
        return (r.text or "").strip()
    except Exception:
        return ""

def _find_element_xy(element: str):
    img, raw = _screenshot_image()
    w, h = img.size
    prompt = (f"This is a {w}x{h} Windows screen capture. Locate: '{element}'. "
              f"Return ONLY a JSON object, no other text: "
              f'{{"found": true, "x": <center_x_pixel>, "y": <center_y_pixel>, "what": "<short description>"}} '
              f'If not visible: {{"found": false}}. Coordinates must be in the {w}x{h} space.')
    if GROQ_VISION_MODEL:
        b64 = base64.b64encode(raw).decode()
        try:
            r = groq.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
                max_tokens=120, temperature=0, response_format={"type": "json_object"})
            j = json.loads(r.choices[0].message.content)
            if j.get("found") and isinstance(j.get("x"), (int, float)) and isinstance(j.get("y"), (int, float)):
                return max(0, min(int(j["x"]), w - 1)), max(0, min(int(j["y"]), h - 1)), j.get("what", element)
        except Exception as e:
            print(f"  [vision locate failed: {str(e)[:60]} — trying gemini]")
    try:
        r = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[types.Part.from_bytes(data=raw, mime_type="image/png"), prompt])
        txt = (r.text or "").strip()
        m = re.search(r'\{.*\}', txt, re.S)
        if m:
            j = json.loads(m.group(0))
            if j.get("found") and isinstance(j.get("x"), (int, float)):
                return max(0, min(int(j["x"]), w - 1)), max(0, min(int(j["y"]), h - 1)), j.get("what", element)
    except Exception as e:
        print(f"  [gemini locate failed: {str(e)[:60]}]")
    return None

def click_screen(app: str = "", element: str = "") -> str:
    """TIER-2 CLICKER (vision): screenshot + coordinates + physical click. For anything UIA can't see."""
    if not element:
        return "Tell me what to click (element is required)."
    try:
        import pyautogui
    except ImportError:
        return "The pyautogui package is needed — tell the user to install it."
    if app and not _focus_window(app):
        return f"Couldn't focus '{app}'. Ask the user to click it, then repeat. Do NOT claim success."
    time.sleep(0.3)
    loc = _find_element_xy(element)
    if not loc:
        return (f"Couldn't find '{element}' on screen (vision unavailable or element not visible). "
                f"Try ui_click/ui_list first for standard apps. Do NOT claim success.")
    x, y, what = loc
    try:
        pyautogui.moveTo(x, y, duration=0.35)
        time.sleep(0.15)
        pyautogui.click()
        time.sleep(0.6)
        after = _screenshot_text(
            f"You are Tony verifying a click. The mouse just clicked '{element}' at ({x},{y}). "
            f"In max 2 short sentences, state what is on screen now and whether it worked.")
        return f"Clicked '{what}' at ({x},{y}). Screen now: {after or 'could not verify'}"
    except Exception as e:
        return f"Click failed: {str(e)[:80]}"

def type_text(text: str, app: str = "") -> str:
    """Type into the FOCUSED window. Click the target box first (ui_click or click_screen), then type_text."""
    try:
        import pyautogui
    except ImportError:
        return "The pyautogui package is needed — tell the user to install it."
    if app and not _focus_window(app):
        return f"Couldn't focus '{app}'. Ask the user to click the window first."
    try:
        pyautogui.write(text, interval=0.03)
        return f"Typed {len(text)} characters into the focused window."
    except Exception as e:
        return f"Typing failed: {str(e)[:80]}"

# ---------- TIMESTAMP PARSER ----------
def _parse_timestamp(text: str):
    t = text.lower()
    m = re.search(r'(\d{1,2}):(\d{1,2})', t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    total = 0
    mm = re.search(r'(\d+)\s*(?:minutes?|mins?|min\.?)\b', t)
    ss = re.search(r'(\d+)\s*(?:seconds?|secs?|sec)\b', t)
    if mm: total += int(mm.group(1)) * 60
    if ss: total += int(ss.group(1))
    if total == 0:
        b = re.search(r'(?:from|at|start(?:ing)? (?:from|at)?)\s+(\d{1,4})\b', t)
        if b:
            total = int(b.group(1))
    return total if total > 0 else None

def _strip_timestamp(text: str) -> str:
    t = text.lower()
    t = re.sub(r'\d{1,2}:\d{1,2}', ' ', t)
    t = re.sub(r'\b(?:from|at|start(?:ing)?(?: from| at)?)\s+\d{1,4}\s*(?:minutes?|mins?|min\.?|seconds?|secs?|sec)?\b', ' ', t)
    t = re.sub(r'\d{1,4}\s*(?:minutes?|mins?|min\.?|seconds?|secs?|sec)\b', ' ', t)
    t = re.sub(r'\b(?:start|starting|begin|from|at)\b', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()

# ---------- TOOLS ----------
def web_search(query: str) -> str:
    """Silently research a topic (for ANSWERING the user verbally). Use for 'what is X', news, research."""
    r = tavily.search(query, max_results=4)
    return "\n".join(f"{x['title']}: {x['content'][:200]} (url: {x['url']})" for x in r["results"]) or "No results."

def browser_search(query: str) -> str:
    """Open a VISIBLE Google search results tab in the ACTIVE Chrome profile. For plain 'search X'."""
    from urllib.parse import quote_plus
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    _launch_chrome(url)
    STATE["last_site"] = f"google search: {query}"
    return f"Opened Google search for '{query}' in the active Chrome profile."

def open_on_site(site: str, what: str) -> str:
    """THE tool for 'go to SITE and find/play/search THING'. Target-domain results only. YouTube timestamps become &t= URLs."""
    site = site.strip().replace("https://", "").replace("http://", "").strip("/")
    domain = site.replace("www.", "")
    is_youtube = "youtube" in domain or "youtu.be" in domain
    query, ts = what, None
    if is_youtube:
        ts = _parse_timestamp(what)
        if ts:
            query = _strip_timestamp(what) or what
    def on_domain(u: str) -> bool:
        u = u.lower()
        if domain in u or f"www.{domain}" in u:
            return True
        return is_youtube and "youtu.be" in u
    r = tavily.search(f"site:{domain} {query}", max_results=8)
    results = [x for x in r.get("results", []) if on_domain(x.get("url", ""))]
    if results:
        words = [w for w in query.lower().split() if len(w) > 2]
        def score(x):
            blob = (x.get("url", "") + " " + x.get("title", "")).lower()
            s = sum(1 for w in words if w in blob)
            if is_youtube and ("watch" in x.get("url", "") or "/shorts/" in x.get("url", "")):
                s += 5
            if "wikipedia" in blob:
                s -= 100
            return s
        best = max(results, key=score)
        url, title = best["url"], best.get("title", "")
        note = f"Opened the exact page: '{title}' at {url}"
        if ts:
            sep = "&t=" if "?" in url else "?t="
            url += sep + f"{ts}s"
            note = f"Opened '{title}' starting at {ts} seconds: {url}"
        _launch_chrome(url)
        STATE["last_site"] = url
        return note + " — user is on the target page."
    from urllib.parse import quote_plus
    if is_youtube:
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        _launch_chrome(url)
        STATE["last_site"] = url
        return (f"No direct video found via Tavily; opened YouTube's own search results for "
                f"'{query}'. Offer to click the video with ui_click('chrome', '<video title>').")
    url = f"https://www.google.com/search?q=site:{domain}+{quote_plus(query)}"
    _launch_chrome(url)
    STATE["last_site"] = url
    return (f"No direct page found on {domain}; opened a Google site-search. "
            f"Offer to click a result with ui_click.")

def open_app(app: str) -> str:
    """Open ANY application by name. Known shortcuts for common ones; anything else goes through Windows' app launcher."""
    name = app.lower().strip()
    cmd = APPS.get(name)
    if not cmd:
        for cand in [name] + name.split():
            m = difflib.get_close_matches(cand, APPS.keys(), n=1, cutoff=0.4)
            if m:
                name = m[0]; cmd = APPS[name]; break
    if cmd:
        subprocess.run(cmd, shell=True)
    else:
        subprocess.run(f'start {name}', shell=True)
        return (f"Tried to open '{name}' via Windows. If nothing opened, it is not installed — tell the user.")
    if name == "chrome":
        STATE["chrome_running"] = True
    return f"Opened {name}. You can now ui_click or ui_list its elements."

def open_website(url: str, browser: str = "chrome") -> str:
    """Open a website HOMEPAGE. browser: 'chrome' (ACTIVE profile), 'brave', or 'edge'."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    b = browser.lower().strip()
    if b in ("brave", "edge"):
        exe = "brave" if b == "brave" else "msedge"
        subprocess.run(f'start {exe} "{url}"', shell=True)
        out = f"Opened {url} in {b.capitalize()}."
    else:
        _launch_chrome(url)
        out = f"Opened {url} in Chrome profile '{STATE['chrome_profile']}'."
    STATE["last_site"] = url
    return out

def open_chrome_profile(profile: str) -> str:
    """Switch Chrome to a named profile. Closes gently (session saved), relaunches, waits ready, sends Ctrl+Shift+T. Never types passwords."""
    user_data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    profiles = {}
    try:
        with open(os.path.join(user_data, "Local State"), encoding="utf-8") as f:
            info = json.load(f).get("profile", {}).get("info_cache", {})
        for dirname, meta in info.items():
            profiles[dirname] = meta.get("name") or meta.get("user_name") or dirname
    except Exception:
        pass
    want = profile.lower().strip()
    for dirname, name in profiles.items():
        if want and (want in name.lower() or name.lower() in want):
            was_running = _chrome_running()
            if was_running:
                _close_chrome_gently()
                time.sleep(1.0)
            subprocess.run(f'start chrome --profile-directory="{dirname}" {CHROME_FLAG}', shell=True)
            STATE["chrome_running"] = True
            STATE["chrome_profile"] = name
            ready = _wait_for_window_ready("chrome", 15)
            restore_note = ""
            if ready:
                try:
                    import pyautogui
                    pyautogui.hotkey("ctrl", "shift", "t")
                    time.sleep(2.0)
                    restore_note = " Sent Ctrl+Shift+T after the window was ready."
                except Exception:
                    restore_note = " Couldn't send the restore shortcut."
            else:
                restore_note = " Chrome window didn't appear within 15s."
            return (f"Chrome {'was closed cleanly and ' if was_running else ''}relaunched into "
                    f"profile '{name}' (saved logins active).{restore_note} "
                    f"If tabs didn't return, enable Settings > On startup > 'Continue where you left off' once.")
    listing = ", ".join(f"{v} [{k}]" for k, v in profiles.items()) or "none found"
    return f"No Chrome profile matching '{profile}'. Available: {listing}"

def restore_tabs() -> str:
    """Restore Chrome's last session: waits for ready, focuses, sends Ctrl+Shift+T."""
    if not _chrome_running():
        _launch_chrome()
    ready = _wait_for_window_ready("chrome", 15)
    if not ready:
        return "Couldn't find a ready Chrome window. Open Chrome, click it, then say 'restore tabs' again."
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "shift", "t")
        time.sleep(2.0)
        return ("Sent Ctrl+Shift+T to fully-loaded Chrome. Saying 'restore again' digs one layer deeper.")
    except ImportError:
        return "The pyautogui package is needed — tell the user to install it."

def press_hotkey(keys: str, app: str = "") -> str:
    """Press a keyboard shortcut on the focused window. keys like 'ctrl+t', 'f5', 'ctrl+shift+t', 'alt+tab', 'win+d'."""
    try:
        import pyautogui
    except ImportError:
        return "The pyautogui package is needed — tell the user to install it."
    if app and not _focus_window(app):
        return f"Couldn't focus '{app}' — ask the user to click it first."
    combo = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not combo:
        return f"Couldn't parse the shortcut '{keys}'."
    try:
        pyautogui.hotkey(*combo)
        return f"Pressed {'+'.join(combo)}. Ask the user to confirm the effect."
    except Exception as e:
        return f"Hotkey failed: {str(e)[:80]}"

def close_window(what: str) -> str:
    """Close an app's windows. Chrome closed gently (session saved)."""
    n = what.lower()
    if "chrome" in n:
        _close_chrome_gently()
        STATE["chrome_running"] = False
        STATE["chrome_profile"] = "default"
        return "Closed Chrome cleanly — session saved, tabs recoverable."
    if "edge" in n:
        subprocess.run('taskkill /IM msedge.exe /F', shell=True, capture_output=True)
        return "Closed Edge."
    for known in EXES:
        if known in n:
            subprocess.run(f'taskkill /IM {EXES[known]} /F', shell=True, capture_output=True)
            return f"Closed {known}."
    guess = n.split()[0] if n.split() else n
    subprocess.run(f'taskkill /IM {guess}.exe /F', shell=True, capture_output=True)
    return f"Tried closing '{guess}'. If still open, name the app differently."

def scroll_screen(direction: str = "down", seconds: float = 3, speed: str = "normal") -> str:
    """Scroll the mouse wheel on the FOCUSED window. direction: 'up'/'down'. speed: 'slow'/'normal'/'fast'."""
    try:
        import pyautogui
    except ImportError:
        return "The pyautogui package is needed — tell the user to install it."
    step = 5 if direction.lower().startswith("up") else -5
    if speed.lower() == "fast":
        step *= 4
    delay = 0.5 if speed.lower() == "slow" else (0.1 if speed.lower() == "fast" else 0.25)
    try:
        pyautogui.moveTo(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2, duration=0.2)
    except Exception:
        pass
    end = time.time() + max(0.5, min(float(seconds), 30))
    n = 0
    try:
        while time.time() < end:
            pyautogui.scroll(step)
            n += 1
            time.sleep(delay)
    except Exception as e:
        return f"Scroll failed: {str(e)[:80]}"
    return (f"Scrolled {direction} for ~{seconds}s on the focused window. "
            f"If nothing moved, the window wasn't focused — tell the user to click it first.")

def _screenshot_q(instruction: str) -> str:
    return _screenshot_text(instruction)

def look_at_screen(question: str) -> str:
    """Screenshot + describe. ONLY when the user explicitly asks about the screen."""
    out = _screenshot_q("You are Tony looking at the user's Windows screen. Answer in max 2 short spoken sentences: " + question)
    return out or "Screen check unavailable (no vision model available right now)."

def verify_screen(expectation: str) -> str:
    """Screenshot + check. ONLY when the user says something looks wrong."""
    out = _screenshot_q(
        f"You are Tony checking the user's screen. Is the following true? '{expectation}'. "
        f"Answer in max 2 short sentences starting with SEE: or NOT-SEE:")
    return out or "Screen check unavailable (no vision model available right now)."

def run_command(command: str) -> str:
    """Run a shell command. Chrome has NO tab-restore flags."""
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    if "chrome.exe" in command and "taskkill" in command and "/f" in command.lower():
        STATE["chrome_running"] = False
        STATE["chrome_profile"] = "default"
    return (r.stdout or r.stderr or "done")[:1500]

TOOLS = {"web_search": web_search, "browser_search": browser_search, "open_on_site": open_on_site,
         "open_app": open_app, "open_website": open_website, "open_chrome_profile": open_chrome_profile,
         "close_window": close_window, "restore_tabs": restore_tabs, "press_hotkey": press_hotkey,
         "type_text": type_text, "ui_list": ui_list, "ui_click": ui_click,
         "click_screen": click_screen, "scroll_screen": scroll_screen,
         "look_at_screen": look_at_screen, "verify_screen": verify_screen, "run_command": run_command}

GROQ_TOOLS = [
    {"type": "function", "function": {"name": "web_search",
        "description": "Silently research a topic (for ANSWERING verbally). Use for 'what is X', news, research.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "browser_search",
        "description": "Open a VISIBLE Google search tab in the ACTIVE Chrome profile. For plain 'search X'.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "open_on_site",
        "description": "THE tool for 'go to SITE and find/play/search THING'. Opens the EXACT page directly. YouTube timestamps become &t= URLs.",
        "parameters": {"type": "object",
                       "properties": {"site": {"type": "string"},
                                      "what": {"type": "string"}},
                       "required": ["site", "what"]}}},
    {"type": "function", "function": {"name": "open_app",
        "description": "Open ANY application by name (chrome, spotify, notepad, word, games — anything installed).",
        "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "open_website",
        "description": "Open a website HOMEPAGE. browser: 'chrome' (ACTIVE profile), 'brave', or 'edge'.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"},
                                      "browser": {"type": "string", "enum": ["chrome", "brave", "edge"]}},
                       "required": ["url"]}}},
    {"type": "function", "function": {"name": "open_chrome_profile",
        "description": "Switch Chrome to a named profile with saved logins; gentle close + tab restore. Use for 'login with my X account'.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}}, "required": ["profile"]}}},
    {"type": "function", "function": {"name": "restore_tabs",
        "description": "Restore Chrome's last session (waits ready + Ctrl+Shift+T).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "press_hotkey",
        "description": "Press a keyboard shortcut on the focused window: 'ctrl+t', 'f5', 'alt+tab', 'win+d', etc.",
        "parameters": {"type": "object",
                       "properties": {"keys": {"type": "string"},
                                      "app": {"type": "string"}},
                       "required": ["keys"]}}},
    {"type": "function", "function": {"name": "type_text",
        "description": "Type into the FOCUSED window. Click the target box first (ui_click or click_screen), then type.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"},
                                      "app": {"type": "string"}},
                       "required": ["text"]}}},
    {"type": "function", "function": {"name": "ui_list",
        "description": "List every clickable element BY NAME in ANY open app (chrome, spotify, word, anything). Use before ui_click when unsure of exact names.",
        "parameters": {"type": "object",
                       "properties": {"app": {"type": "string"}},
                       "required": ["app"]}}},
    {"type": "function", "function": {"name": "ui_click",
        "description": "TIER-1 universal clicker: click a control BY NAME in ANY open app via Windows UI Automation. Precise, instant, offline. 'click play in spotify' -> ui_click('spotify', 'Play'). Fall back to click_screen if not found.",
        "parameters": {"type": "object",
                       "properties": {"app": {"type": "string"},
                                      "element_name": {"type": "string"}},
                       "required": ["app", "element_name"]}}},
    {"type": "function", "function": {"name": "click_screen",
        "description": "TIER-2 vision clicker: screenshot + coordinates + click. For apps/content UIA can't see. Try ui_click first for standard apps.",
        "parameters": {"type": "object",
                       "properties": {"app": {"type": "string"},
                                      "element": {"type": "string"}},
                       "required": ["element"]}}},
    {"type": "function", "function": {"name": "close_window",
        "description": "Close an app's windows. Chrome closed gently (session saved).",
        "parameters": {"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]}}},
    {"type": "function", "function": {"name": "scroll_screen",
        "description": "Scroll the mouse wheel on the FOCUSED window. direction (up/down), seconds, speed.",
        "parameters": {"type": "object",
                       "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                                      "seconds": {"type": "number"},
                                      "speed": {"type": "string", "enum": ["slow", "normal", "fast"]}},
                       "required": ["direction"]}}},
    {"type": "function", "function": {"name": "look_at_screen",
        "description": "Screenshot + describe. ONLY when the user explicitly asks about the screen.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {"name": "verify_screen",
        "description": "Screenshot + check. ONLY when the user says something looks wrong.",
        "parameters": {"type": "object", "properties": {"expectation": {"type": "string"}}, "required": ["expectation"]}}},
    {"type": "function", "function": {"name": "run_command",
        "description": "Run a shell command. Chrome has NO tab-restore flags.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

# ---------- PRIMARY BRAIN ----------
def _groq_call(messages):
    last = None
    for _ in range(2):
        try:
            return groq.chat.completions.create(
                model=BRAIN_MODEL, messages=messages, tools=GROQ_TOOLS,
                temperature=0.2, max_tokens=250)
        except Exception as e:
            last = e
            print("  (groq hiccup — retrying)")
            time.sleep(2)
    raise last

def _tool_loop(messages):
    final, actions = None, []
    for _ in range(8):
        m = _groq_call(messages).choices[0].message
        if not m.tool_calls:
            final = (m.content or "").strip()
            break
        messages.append(m)
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            print(f"  [tool: {tc.function.name} {args}]")
            fn = TOOLS.get(tc.function.name)
            out = fn(**args) if fn else f"Unknown tool: {tc.function.name}"
            actions.append(f"{tc.function.name}({args}) -> {str(out)[:80]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)[:1200]})
    return final, actions

def brain_groq(msg: str) -> str:
    messages = [{"role": "system", "content": system_prompt()}] + HISTORY + [{"role": "user", "content": msg}]
    final, actions = _tool_loop(messages)
    if final is None or final == "" or (not actions and final.lower().strip(" .!") in ("done", "ok", "okay")):
        print("  (no-op detected — nudging brain)")
        messages.append({"role": "user",
                         "content": "Your last reply did nothing. Resolve intent: ui_click is the precise "
                                    "universal clicker for any app (use ui_list first if unsure of names), "
                                    "click_screen for vision, open_on_site for site searches — then answer."})
        final, actions = _tool_loop(messages)
    final = final or "Done."
    final = re.sub(r"\s*\[Done this turn:[^\]]*\]", "", final).strip()
    if actions:
        final += f" [Done this turn: {'; '.join(actions)}]"
    remember("user", msg)
    remember("assistant", final)
    return final

# ---------- FALLBACK BRAIN: Gemini, quota-aware ----------
def brain_gemini(msg: str):
    history = [{"role": "model" if t["role"] == "assistant" else "user",
                "parts": [{"text": t["content"]}]} for t in HISTORY]
    for _ in range(2):
        try:
            chat = client.chats.create(
                model="gemini-3.8-flash",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt(),
                    tools=[web_search, browser_search, open_on_site, open_app, open_website,
                           open_chrome_profile, close_window, restore_tabs, press_hotkey,
                           type_text, ui_list, ui_click, click_screen, scroll_screen,
                           look_at_screen, verify_screen, run_command]),
                history=history)
            answer = (chat.send_message(msg).text or "Done.").strip()
            remember("user", msg)
            remember("assistant", answer)
            return answer
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print("  (gemini daily quota exhausted — skipping fallback)")
                return None
            print("  (gemini busy — retrying)")
            time.sleep(2)
    return None

def ask_tony(msg: str) -> str:
    t0 = time.time()
    try:
        answer = brain_groq(msg)
        print(f"  [brain: groq/{BRAIN_MODEL}, {time.time()-t0:.1f}s]")
        return answer
    except Exception as e:
        print(f"  [groq failed: {e} — falling back to gemini]")
    answer = brain_gemini(msg)
    if answer:
        print(f"  [brain: gemini-fallback, {time.time()-t0:.1f}s]")
        return answer
    return "My fast brain is overloaded and my backup is out of daily quota. Try again in a bit."

# ---------- EARS (voice input) ----------
VOICE_INPUT = True        # Enter on an empty prompt = speak. False = typing only.
HANDS_FREE = False        # True = always listening, no Enter needed (Ctrl+C to stop)
STT_LANGUAGE = "en"       # None = let Whisper auto-detect the language
MIC_THRESHOLD = 500       # raise if it hears background noise, lower if it misses your voice
STT_PROMPT = ("Voice commands for a PC assistant named Tony: open Claude, open " + ", open ".join(APPS) +
              ", open YouTube, open Google, play song on YouTube, search, scroll down, scroll up, close, restore tabs.")

def _pick_stt_model():
    for p in ("whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"):
        if p in AVAILABLE_MODELS:
            return p
    return "whisper-large-v3"

STT_MODEL = _pick_stt_model()

def listen(max_seconds=15, silence_seconds=1.2, wait_seconds=8) -> str:
    """Record from the mic until you stop talking, then transcribe with Groq Whisper."""
    try:
        import sounddevice as sd, numpy as np, wave
    except ImportError:
        print("  [voice input needs: pip install sounddevice numpy]")
        return ""
    from collections import deque
    rate, chunk_s = 16000, 0.1
    chunk = int(rate * chunk_s)
    pre, frames = deque(maxlen=8), []   # ~0.8s of audio kept from before speech is detected
    started, quiet, t_start = False, 0, 0.0
    print("  [listening... speak now]")
    try:
        with sd.InputStream(samplerate=rate, channels=1, dtype="int16") as stream:
            t0 = time.time()
            while True:
                data, _ = stream.read(chunk)
                level = int(np.abs(data.astype(np.int32)).mean())
                if not started:
                    pre.append(data)
                    if level > MIC_THRESHOLD:
                        started, t_start = True, time.time()
                        frames.extend(pre)
                    elif time.time() - t0 > wait_seconds:
                        return ""
                else:
                    frames.append(data)
                    quiet = 0 if level > MIC_THRESHOLD else quiet + 1
                    if quiet >= int(silence_seconds / chunk_s) or time.time() - t_start > max_seconds:
                        break
    except Exception as e:
        print(f"  [mic error: {str(e)[:80]}]")
        return ""
    audio = np.concatenate(frames)
    peak = int(np.abs(audio).max()) if len(audio) else 0
    if 0 < peak < 20000:   # quiet mic: boost toward a healthy level (capped at 8x so noise isn't blown up)
        audio = np.clip(audio.astype(np.float32) * min(24000 / peak, 8.0), -32768, 32767).astype(np.int16)
    print(f"  [heard {len(audio)/rate:.1f}s, mic peak {peak}]")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    try:
        kwargs = dict(file=("speech.wav", buf.getvalue()), model=STT_MODEL, temperature=0, prompt=STT_PROMPT)
        if STT_LANGUAGE:
            kwargs["language"] = STT_LANGUAGE
        r = groq.audio.transcriptions.create(**kwargs)
        text = (getattr(r, "text", r) or "").strip()
    except Exception as e:
        print(f"  [transcription failed: {str(e)[:80]}]")
        return ""
    text = text.strip(" .!?,")
    if text.lower() in ("", "you", "thank you", "thanks for watching"):   # Whisper's silence hallucinations
        return ""
    return text

# ---------- MAIN LOOP ----------
print("Tony ready (memory + UIA universal control + vision fallback). Type 'quit' to exit.\n")
speak("Tony online.")
while True:
    if VOICE_INPUT and HANDS_FREE:
        msg = listen()
    else:
        msg = input("You (Enter = speak, or type): " if VOICE_INPUT else "You: ").strip()
        if not msg and VOICE_INPUT:
            msg = listen()
    if msg and VOICE_INPUT:
        print(f"You: {msg}")
    if not msg:
        continue
    if msg.lower() in ("quit", "exit", "goodbye"):
        speak("Going offline. Goodbye, sir.")
        break
    if msg.lower().startswith("forget"):
        HISTORY.clear()
        STATE.update({"chrome_running": _chrome_running(), "chrome_profile": "default", "last_site": "none"})
        if os.path.exists(MEMORY_FILE):
            os.remove(MEMORY_FILE)
        print("[memory] wiped.")
        speak("Memory wiped.")
        continue
    t_start = time.time()
    answer = ask_tony(msg)
    print("Tony:", answer)
    speak(re.sub(r"\s*\[Done this turn:[^\]]*\]", "", answer).strip())   # don't read the debug note aloud
    print(f"  [TOTAL: {time.time()-t_start:.1f}s]\n")