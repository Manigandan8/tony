# =====================================================================
# tony_core/vision.py
# Vision processing: screenshot capture, VLM provider fallback chain,
# Set-of-Mark (SoM) visual grounding, column-aware OCR & UIA locator.
#
# SHARED MUTABLE GLOBALS REFERENCED AND MUTATED ACROSS PROVIDERS:
# - groqai.GROQ_VISION_MODEL (rebound on 404/retirement)
# - github_models.GITHUB_VISION_MODELS (reordered / items removed)
# - zai.ZAI_VISION_MODELS (reordered / items removed)
# - zai._ZAI_VISION_WAIT (cooldown timestamp)
# - mistralai.MISTRAL_VISION_MODELS (reordered / items removed)
# - gemini.GEMINI_VISION_MODELS (reordered / items removed)
# - cohereai.COHERE_VISION_MODELS (reordered / items removed)
# - openrouter.OR_VISION_MODELS (reordered / items removed)
# =====================================================================

import os
import io
import re
import time
import json
import ctypes
import base64
import difflib
import platform
from google.genai import types as gemini_types

from tony_core import config
from tony_core.net import HTTP
from tony_core.providers import groqai, github_models, zai, mistralai, gemini, cohereai, openrouter

if platform.system() == "Windows":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

VISION_MAX_W = config.VISION_MAX_W
OCR_ENABLED = config.OCR_ENABLED
VISION_BUDGET_S = config.VISION_BUDGET_S

_ORDINAL_OR_VISUAL_PAT = re.compile(
    r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|bottom|left|right|"
    r"middle|center|next|previous|last|thumbnail|icon|button|play button|skip ad)\b",
    re.I
)
_GENERIC_STOPWORDS = {
    "the", "a", "an", "on", "in", "at", "to", "for", "of", "from", "with",
    "video", "videos", "song", "music", "movie", "clip", "click", "open",
    "play", "watch", "select", "tap", "screen", "youtube", "now", "please"
}


def _screenshot_image():
    from PIL import Image
    try:
        import mss
        with mss.mss() as sct:
            raw_sct = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw_sct.size, raw_sct.bgra, "raw", "BGRX")
    except Exception:
        from PIL import ImageGrab
        img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return img, buf.getvalue()


def _vision_shot():
    img, _ = _screenshot_image()
    small = img.resize((VISION_MAX_W, int(img.height * VISION_MAX_W / img.width))) if img.width > VISION_MAX_W else img
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=85)
    return img, buf.getvalue(), img.width / small.width


def _setup_tesseract(pytesseract):
    import shutil
    if shutil.which("tesseract"):
        return
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(p):
            pytesseract.pytesseract.tesseract_cmd = p
            return


def _active_region(img):
    try:
        import pyautogui
        w = pyautogui.getActiveWindow()
        if w and "tony" not in (w.title or "").lower() and w.width > 200 and w.height > 200:
            l, t = max(0, w.left), max(0, w.top)
            r, b = min(img.width, w.left + w.width), min(img.height, w.top + w.height)
            if r - l > 200 and b - t > 200:
                return (l, t, r, b)
    except Exception:
        pass
    return None


def _is_in_browser_top_chrome(y: int, screen_h: int) -> bool:
    """Rejects hits inside browser tabs, URL bar, YouTube search bar, and category chips."""
    return y < int(screen_h * 0.18)


def _split_ocr_into_phrases(data, min_conf=42):
    """
    Groups OCR words into lines, then splits any line where consecutive words have
    a horizontal gap > 28px. This prevents side-by-side YouTube video columns from
    merging into a single wide bounding box whose midpoint lands in empty space!
    """
    raw_lines = {}
    for i, raw in enumerate(data["text"]):
        wd = (raw or "").strip()
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        if wd and conf >= min_conf:
            raw_lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)

    phrases = []
    for idxs in raw_lines.values():
        idxs_sorted = sorted(idxs, key=lambda idx: data["left"][idx])
        curr = [idxs_sorted[0]]
        for idx in idxs_sorted[1:]:
            prev = curr[-1]
            prev_right = data["left"][prev] + data["width"][prev]
            gap = data["left"][idx] - prev_right
            # If gap > 28px, it belongs to a different column/video card!
            if gap > 28:
                phrases.append(curr)
                curr = [idx]
            else:
                curr.append(idx)
        if curr:
            phrases.append(curr)
    return phrases


def _safe_click_point(l: int, t: int, r: int, b: int):
    """
    Returns a click point on the left-third of a text phrase rather than dead center,
    avoiding empty trailing space or 3-dot menu icons on YouTube cards.
    """
    width = max(1, r - l)
    cx = l + min(75, max(12, width // 3))
    cy = (t + b) // 2
    return cx, cy


def _match_ocr(data, want, offset_y=0, screen_h=1080):
    want_clean = re.sub(r"\s+", " ", (want or "").lower().strip())
    if not want_clean or _ORDINAL_OR_VISUAL_PAT.search(want_clean):
        return None

    wl = [w.strip(".,:;!?()[]{}'\"") for w in want_clean.split() if w.strip(".,:;!?()[]{}'\"")]
    sig = [w for w in wl if w not in _GENERIC_STOPWORDS and (len(w) > 1 or w.isdigit())]
    if not sig:
        return None

    phrases = _split_ocr_into_phrases(data, min_conf=40)
    best = None

    for idxs in phrases:
        t_min = min(data["top"][i] for i in idxs) + offset_y
        b_max = max(data["top"][i] + data["height"][i] for i in idxs) + offset_y
        cy = (t_min + b_max) // 2
        if _is_in_browser_top_chrome(cy, screen_h):
            continue

        words = [data["text"][i].strip().lower().strip(".,:;!?()[]{}'\"") for i in idxs]
        phrase_str = " ".join(words)
        hit, score = None, 0.0

        # 1) Exact phrase match
        for s in range(len(words) - len(sig) + 1):
            if words[s:s + len(sig)] == sig:
                hit, score = idxs[s:s + len(sig)], 260.0
                break

        # 2) Multi-word phonetic / fuzzy match (handles Whisper misspellings like "Dola Yei" -> "Dola Re")
        if hit is None:
            matched_idxs = []
            exact_hits = 0
            for sw in sig:
                best_k, best_r = None, 0.0
                for k, w in enumerate(words):
                    if w == sw or (len(sw) >= 4 and w.startswith(sw[:4])):
                        best_k, best_r = k, 1.0
                        break
                    r = difflib.SequenceMatcher(None, w, sw).ratio()
                    if r > best_r:
                        best_k, best_r = k, r
                if best_r >= 0.72:
                    matched_idxs.append(idxs[best_k])
                    if best_r >= 0.92:
                        exact_hits += 1

            if len(sig) >= 2 and len(matched_idxs) >= 2:
                hit = matched_idxs
                score = 170.0 + (len(matched_idxs) / len(sig)) * 40.0
            elif len(sig) >= 2 and exact_hits >= 1 and len(sig[0]) >= 4:
                # Anchor word (e.g. "dola") matched strongly even if 2nd word ("yei") was Whisper noise
                whole_ratio = difflib.SequenceMatcher(None, " ".join(sig), phrase_str).ratio()
                if whole_ratio >= 0.52 or any(w == sig[0] for w in words):
                    hit = idxs
                    score = 135.0 + whole_ratio * 30.0
            elif len(sig) == 1 and len(matched_idxs) == 1:
                hit = matched_idxs
                score = 120.0

        if hit is not None:
            avg_conf = sum(float(data["conf"][i]) for i in hit) / len(hit) / 100.0
            score += avg_conf
            if best is None or score > best[0]:
                best = (score, hit)

    if not best:
        return None
    hit = best[1]
    l, t = min(data["left"][i] for i in hit), min(data["top"][i] for i in hit)
    r, b = max(data["left"][i] + data["width"][i] for i in hit), max(data["top"][i] + data["height"][i] for i in hit)
    cx, cy = _safe_click_point(l, t, r, b)
    return cx, cy, " ".join(data["text"][i].strip() for i in hit)


def _ocr_find_xy(text_to_find: str, img):
    if not OCR_ENABLED or _ORDINAL_OR_VISUAL_PAT.search(text_to_find or ""):
        return None
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        _setup_tesseract(pytesseract)
        reg = _active_region(img)
        for region in ([reg] if reg else []) + [None]:
            crop = img.crop(region) if region else img
            ox, oy = (region[0], region[1]) if region else (0, 0)
            data = pytesseract.image_to_data(crop.convert("L"), output_type=pytesseract.Output.DICT)
            hit = _match_ocr(data, text_to_find, offset_y=oy, screen_h=img.height)
            if hit:
                print(f"  [vision answered by: local OCR ('{hit[2]}')]")
                return hit[0] + ox, hit[1] + oy, hit[2]
    except Exception as e:
        print(f"  [ocr failed: {str(e)[:60]}]")
    return None


def _vision_describe(raw: bytes, instruction: str) -> str:
    deadline = time.time() + VISION_BUDGET_S
    b64 = base64.b64encode(raw).decode()

    # 1) Try Gemini first (fastest & most reliable for Set-of-Mark visual grounding)
    if gemini.client:
        for gm in list(gemini.GEMINI_VISION_MODELS):
            if time.time() > deadline:
                return ""
            try:
                r = gemini.client.models.generate_content(
                    model=gm,
                    contents=[gemini_types.Part.from_bytes(data=raw, mime_type="image/jpeg"), instruction])
                out = (r.text or "").strip()
                if out:
                    print(f"  [vision answered by: {gm}]")
                    if gm != gemini.GEMINI_VISION_MODELS[0]:
                        gemini.GEMINI_VISION_MODELS.insert(0, gemini.GEMINI_VISION_MODELS.pop(gemini.GEMINI_VISION_MODELS.index(gm)))
                    return out
            except Exception as e:
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    if gm in gemini.GEMINI_VISION_MODELS:
                        gemini.GEMINI_VISION_MODELS.remove(gm)
                elif "503" in msg or "UNAVAILABLE" in msg or "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    # Demote overloaded model to back of queue so next call doesn't stall on it
                    if gm in gemini.GEMINI_VISION_MODELS and len(gemini.GEMINI_VISION_MODELS) > 1:
                        gemini.GEMINI_VISION_MODELS.append(gemini.GEMINI_VISION_MODELS.pop(gemini.GEMINI_VISION_MODELS.index(gm)))
                    print(f"  [{gm}: busy/overloaded — switching to next]")
                else:
                    print(f"  [{gm}: failed: {msg[:50]}]")

    # 2) Groq Vision (if configured)
    if groqai.GROQ_VISION_MODEL:
        try:
            r = groqai.groq.chat.completions.create(
                model=groqai.GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                max_tokens=300)
            out = (r.choices[0].message.content or "").strip()
            if out:
                print(f"  [vision answered by: {groqai.GROQ_VISION_MODEL}]")
                return out
        except Exception as e:
            msg = str(e)
            if "404" in msg or "decommissioned" in msg.lower() or "not_found" in msg.lower():
                groqai.GROQ_VISION_MODEL = None
            else:
                print(f"  [groq vision failed: {msg[:50]}]")

    # 3) GitHub Models Vision
    if config.GITHUB_TOKEN and github_models.GITHUB_VISION_MODELS:
        for gvm in list(github_models.GITHUB_VISION_MODELS):
            if time.time() > deadline:
                return ""
            try:
                rr = HTTP.post(f"{config.GITHUB_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Content-Type": "application/json"},
                    json={"model": gvm, "max_tokens": 400,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=12)
                if rr.status_code in (400, 404):
                    if gvm in github_models.GITHUB_VISION_MODELS:
                        github_models.GITHUB_VISION_MODELS.remove(gvm)
                    continue
                if rr.status_code != 200:
                    continue
                out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                if out:
                    print(f"  [vision answered by: github/{gvm}]")
                    return out
            except Exception:
                continue

    # 4) Z.ai Vision (with shorter 10s timeout so overloaded 429s never stall Tony)
    if zai.ZAI_API_KEY and zai.ZAI_VISION_MODELS and time.time() >= zai._ZAI_VISION_WAIT["until"]:
        for zvm in list(zai.ZAI_VISION_MODELS):
            if time.time() > deadline:
                return ""
            try:
                rr = HTTP.post(f"{zai._zai_base()}/chat/completions", headers=zai._zai_headers(),
                    json={"model": zvm, "max_tokens": 350, "thinking": {"type": "disabled"},
                          "messages": [{"role": "user", "content": [
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                              {"type": "text", "text": instruction}]}]}, timeout=10)
                if rr.status_code == 200:
                    out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                    if out:
                        print(f"  [vision answered by: zai/{zvm}]")
                        return out
                    continue
                if rr.status_code == 429:
                    zai._ZAI_VISION_WAIT["until"] = time.time() + 120
                    print("  [zai vision busy — cooling down 120s]")
                    break
            except Exception:
                break

    # 5) Mistral / Cohere / OpenRouter Vision fallbacks
    if config.MISTRAL_API_KEY and mistralai.MISTRAL_VISION_MODELS:
        for mvm in list(mistralai.MISTRAL_VISION_MODELS):
            if time.time() > deadline:
                return ""
            try:
                rr = HTTP.post(f"{config.MISTRAL_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {config.MISTRAL_API_KEY}", "Content-Type": "application/json"},
                    json={"model": mvm, "max_tokens": 350,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=12)
                if rr.status_code == 200:
                    out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                    if out:
                        print(f"  [vision answered by: mistral/{mvm}]")
                        return out
            except Exception:
                continue

    if cohereai.COHERE_API_KEY and cohereai.COHERE_VISION_MODELS:
        for cvm in list(cohereai.COHERE_VISION_MODELS):
            if time.time() > deadline:
                return ""
            try:
                rr = HTTP.post(f"{cohereai.COHERE_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {cohereai.COHERE_API_KEY}"},
                    json={"model": cvm, "max_tokens": 250,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=12)
                if rr.status_code == 200:
                    out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                    if out:
                        print(f"  [vision answered by: cohere/{cvm}]")
                        return out
            except Exception:
                continue

    for orm in list(openrouter.OR_VISION_MODELS):
        if time.time() > deadline:
            return ""
        try:
            rr = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {openrouter.OPENROUTER_API_KEY}"},
                json={"model": orm,
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": instruction},
                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                      "max_tokens": 350},
                timeout=12)
            if rr.status_code == 200:
                out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                if out:
                    print(f"  [vision answered by: {orm}]")
                    return out
        except Exception:
            continue
    return ""


def _screenshot_text(instruction: str) -> str:
    _, raw, _scale = _vision_shot()
    return _vision_describe(raw, instruction)


def _collect_som_candidates(img):
    """
    Gathers clickable candidate regions from UIA + column-split OCR phrases.
    Never merges side-by-side YouTube columns into one wide box!
    """
    w, h = img.size
    candidates = []
    seen_points = []

    def _add_cand(name, l, t, r, b, kind="UI"):
        if (r - l) > int(w * 0.48):
            # Reject overly wide multi-column spans
            return
        cx, cy = _safe_click_point(l, t, r, b)
        if _is_in_browser_top_chrome(cy, h):
            return
        if cx < 10 or cy < 10 or cx >= w - 10 or cy >= h - 10:
            return
        for ox, oy in seen_points:
            if abs(cx - ox) < 32 and abs(cy - oy) < 24:
                return
        seen_points.append((cx, cy))
        candidates.append({
            "id": len(candidates) + 1,
            "name": (name or "").strip()[:85],
            "kind": kind,
            "bbox": (max(0, l), max(0, t), min(w - 1, r), min(h - 1, b)),
            "center": (cx, cy)
        })

    # 1) Try Windows UIA interactive elements
    try:
        from pywinauto import Desktop
        top_win = Desktop(backend="uia").top_window()
        for ctrl in top_win.descendants():
            if len(candidates) >= 40:
                break
            try:
                ct = ctrl.element_info.control_type
                if ct not in ("Hyperlink", "Button", "ListItem", "TabItem"):
                    continue
                if not ctrl.is_visible():
                    continue
                rect = ctrl.rectangle()
                bw, bh = rect.width(), rect.height()
                if bw < 24 or bh < 12 or bw > int(w * 0.48) or bh > int(h * 0.5):
                    continue
                txt = (ctrl.window_text() or "").strip()
                if len(txt) < 3:
                    continue
                _add_cand(txt, rect.left, rect.top, rect.right, rect.bottom, kind=ct)
            except Exception:
                continue
    except Exception:
        pass

    # 2) Supplement with column-split OCR phrases
    if len(candidates) < 15 and OCR_ENABLED:
        try:
            import pytesseract
            _setup_tesseract(pytesseract)
            data = pytesseract.image_to_data(img.convert("L"), output_type=pytesseract.Output.DICT)
            phrases = _split_ocr_into_phrases(data, min_conf=45)
            for idxs in phrases:
                if len(candidates) >= 55:
                    break
                text_line = " ".join(data["text"][i].strip() for i in idxs).strip()
                if len(text_line) < 4:
                    continue
                l = min(data["left"][i] for i in idxs)
                t = min(data["top"][i] for i in idxs)
                r = max(data["left"][i] + data["width"][i] for i in idxs)
                b = max(data["top"][i] + data["height"][i] for i in idxs)
                _add_cand(text_line, l, t, r, b, kind="Text")
        except Exception:
            pass

    return candidates


def _som_locate(element: str, img=None):
    """
    Set-of-Mark (SoM) Visual Grounding with column-split bounding boxes.
    """
    from PIL import ImageDraw
    if img is None:
        img, _ = _screenshot_image()
    w, h = img.size
    candidates = _collect_som_candidates(img)
    if not candidates:
        return None

    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    id_map = {}

    for c in candidates:
        cid = c["id"]
        id_map[cid] = c
        l, t, r, b = c["bbox"]
        draw.rectangle([l, t, r, b], outline=(255, 0, 85), width=2)
        tag = str(cid)
        tw = len(tag) * 8 + 8
        th = 18
        bx1, by1 = max(0, l), max(0, t - th)
        bx2, by2 = min(w - 1, bx1 + tw), by1 + th
        draw.rectangle([bx1, by1, bx2, by2], fill=(255, 0, 85))
        draw.text((bx1 + 4, by1 + 2), tag, fill=(255, 255, 255))

    scale = w / VISION_MAX_W if w > VISION_MAX_W else 1.0
    sw, sh = int(w / scale), int(h / scale)
    small = annotated.resize((sw, sh)) if scale > 1.0 else annotated
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=85)

    catalog_lines = [f"[{c['id']}] {c['name']}" for c in candidates[:50] if c["name"]]
    catalog_text = "\n".join(catalog_lines)

    prompt = (
        f"This is a {sw}x{sh} screenshot annotated with numbered red boxes [1], [2], [3], etc.\n"
        f"The user wants to click: '{element}' (note: speech recognition may have slightly misspelled foreign/song words).\n"
        f"Candidate labels detected on screen:\n{catalog_text}\n\n"
        f"If a numbered red box [id] is on the target video title, thumbnail, or button, reply ONLY with JSON:\n"
        f'{{"found": true, "id": <int>, "what": "<exact title>"}}\n'
        f"Or if you see the target video thumbnail/title better directly, return the pixel center of the VIDEO THUMBNAIL in the {sw}x{sh} image:\n"
        f'{{"found": true, "x": <int>, "y": <int>, "what": "<exact title>"}}\n'
        f'Or {{"found": false}} if not visible.'
    )

    txt = _vision_describe(buf.getvalue(), prompt)
    m = re.search(r"\{[^{}]*\}", txt or "", re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        if not j.get("found"):
            return None
        if isinstance(j.get("id"), int) and j["id"] in id_map:
            chosen = id_map[j["id"]]
            cx, cy = chosen["center"]
            label = j.get("what") or chosen["name"] or element
            print(f"  [SoM locate: picked badge [{j['id']}] '{label}' @ ({cx},{cy})]")
            return cx, cy, label
        if isinstance(j.get("x"), (int, float)) and isinstance(j.get("y"), (int, float)):
            rx, ry = float(j["x"]), float(j["y"])
            px, py = (int(rx * w), int(ry * h)) if (rx <= 1.0 and ry <= 1.0) else (int(rx * scale), int(ry * scale))
            px, py = max(0, min(px, w - 1)), max(0, min(py, h - 1))
            if not _is_in_browser_top_chrome(py, h):
                return px, py, (j.get("what") or element).strip()
    except Exception:
        pass
    return None


def _find_element_xy(element: str):
    img, raw, scale = _vision_shot()
    som_hit = _som_locate(element, img=img)
    if som_hit:
        return som_hit

    w, h = img.size
    sw, sh = int(w / scale), int(h / scale)
    prompt = (
        f"This is a {sw}x{sh} Windows screenshot. Locate the clickable video thumbnail or title link for: '{element}'. "
        f"Ignore the small 'Tony' overlay window in the top-left corner and NEVER click the top search bar. "
        f"Return ONLY a valid JSON object with pixel coordinates of the center of the video thumbnail in the {sw}x{sh} image: "
        f'{{"found": true, "x": <int>, "y": <int>, "what": "<short description>"}} '
        f'or {{"found": false}} if not visible.'
    )
    txt = _vision_describe(raw, prompt)
    m = re.search(r'\{[^{}]*\}', txt or "", re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            if j.get("found") and isinstance(j.get("x"), (int, float)) and isinstance(j.get("y"), (int, float)):
                rx, ry = float(j["x"]), float(j["y"])
                px, py = (int(rx * w), int(ry * h)) if (rx <= 1.0 and ry <= 1.0) else (int(rx * scale), int(ry * scale))
                return (max(0, min(px, w - 1)), max(0, min(py, h - 1)), j.get("what", element))
        except Exception:
            pass
    return _ocr_find_xy(element, img)