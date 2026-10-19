# =====================================================================
# tony_core/vision.py
# Vision processing: screenshot capture, VLM provider fallback chain, OCR.
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
import base64
import difflib
from google.genai import types as gemini_types

from tony_core import config
from tony_core.net import HTTP
from tony_core.providers import groqai, github_models, zai, mistralai, gemini, cohereai, openrouter

VISION_MAX_W = config.VISION_MAX_W
OCR_ENABLED = config.OCR_ENABLED
VISION_BUDGET_S = config.VISION_BUDGET_S

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
    if shutil.which("tesseract"): return
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
            if r - l > 200 and b - t > 200: return (l, t, r, b)
    except Exception: pass
    return None

def _match_ocr(data, want):
    want = re.sub(r"\s+", " ", (want or "").lower().strip())
    if not want: return None
    wl = want.split()
    sig = [w for w in wl if len(w) > 1 or w.isdigit()] or wl
    lines = {}
    for i, raw in enumerate(data["text"]):
        wd = (raw or "").strip()
        try: conf = float(data["conf"][i])
        except Exception: conf = -1.0
        if wd and conf >= 45:
            lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    best = None
    for idxs in lines.values():
        words = [data["text"][i].strip().lower().strip(".,:;!?()[]{}'\"") for i in idxs]
        hit, score = None, 0.0
        for s in range(len(words) - len(wl) + 1):
            if words[s:s + len(wl)] == wl:
                hit, score = idxs[s:s + len(wl)], 200.0; break
        if hit is None and len(sig) > 1:
            pos = [next((k for k, w in enumerate(words) if w == sw or (len(sw) > 3 and difflib.SequenceMatcher(None, w, sw).ratio() >= 0.82)), None) for sw in sig]
            if all(p is not None for p in pos):
                hit, score = [idxs[p] for p in pos], 140.0
        if hit is None and len(sig) == 1:
            for k, w in enumerate(words):
                if w == sig[0]: hit, score = [idxs[k]], 100.0; break
                if len(w) > 3 and difflib.SequenceMatcher(None, w, sig[0]).ratio() >= 0.85:
                    hit, score = [idxs[k]], 80.0; break
        if hit is not None:
            score += sum(float(data["conf"][i]) for i in hit) / len(hit) / 100
            if best is None or score > best[0]: best = (score, hit)
    if not best: return None
    hit = best[1]
    l, t = min(data["left"][i] for i in hit), min(data["top"][i] for i in hit)
    r, b = max(data["left"][i] + data["width"][i] for i in hit), max(data["top"][i] + data["height"][i] for i in hit)
    return (l + r) // 2, (t + b) // 2, " ".join(data["text"][i].strip() for i in hit)

def _ocr_find_xy(text_to_find: str, img):
    if not OCR_ENABLED: return None
    try: import pytesseract
    except ImportError: return None
    try:
        _setup_tesseract(pytesseract)
        reg = _active_region(img)
        for region in ([reg] if reg else []) + [None]:
            crop = img.crop(region) if region else img
            data = pytesseract.image_to_data(crop.convert("L"), output_type=pytesseract.Output.DICT)
            hit = _match_ocr(data, text_to_find)
            if hit:
                ox, oy = (region[0], region[1]) if region else (0, 0)
                print(f"  [vision answered by: local OCR ('{hit[2]}')]")
                return hit[0] + ox, hit[1] + oy, hit[2]
    except Exception as e:
        print(f"  [ocr failed: {str(e)[:60]}]")
    return None

def _vision_describe(raw: bytes, instruction: str) -> str:
    deadline = time.time() + VISION_BUDGET_S
    b64 = base64.b64encode(raw).decode()
    if groqai.GROQ_VISION_MODEL:
        try:
            r = groqai.groq.chat.completions.create(
                model=groqai.GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                max_tokens=250)
            out = (r.choices[0].message.content or "").strip()
            if out:
                print(f"  [vision answered by: {groqai.GROQ_VISION_MODEL}]")
                return out
        except Exception as e:
            msg = str(e)
            if "404" in msg or "decommissioned" in msg.lower() or "not_found" in msg.lower():
                groqai.GROQ_VISION_MODEL = None
            else: print(f"  [groq vision failed: {msg[:50]}]")
    if config.GITHUB_TOKEN and github_models.GITHUB_VISION_MODELS:
        for gvm in list(github_models.GITHUB_VISION_MODELS):
            if time.time() > deadline: return ""
            try:
                rr = HTTP.post(f"{config.GITHUB_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Content-Type": "application/json"},
                    json={"model": gvm, "max_tokens": 400,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=20)
                if rr.status_code in (400, 404):
                    if gvm in github_models.GITHUB_VISION_MODELS: github_models.GITHUB_VISION_MODELS.remove(gvm)
                    print(f"  [github vision {gvm}: unavailable on this token — {rr.status_code}]")
                    continue
                if rr.status_code != 200: continue
                out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                if out:
                    print(f"  [vision answered by: github/{gvm}]")
                    if gvm != github_models.GITHUB_VISION_MODELS[0]:
                        github_models.GITHUB_VISION_MODELS.insert(0, github_models.GITHUB_VISION_MODELS.pop(github_models.GITHUB_VISION_MODELS.index(gvm)))
                    return out
            except Exception as e:
                print(f"  [github vision failed: {str(e)[:60]}]")
    if zai.ZAI_API_KEY and zai.ZAI_VISION_MODELS and time.time() >= zai._ZAI_VISION_WAIT["until"]:
        for zvm in list(zai.ZAI_VISION_MODELS):
            if time.time() > deadline: return ""
            try:
                def _zai_vision(payload_extra):
                    return HTTP.post(f"{zai._zai_base()}/chat/completions", headers=zai._zai_headers(),
                        json={"model": zvm, "max_tokens": 400,
                              "messages": [{"role": "user", "content": [
                                  {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                  {"type": "text", "text": instruction}]}], **payload_extra}, timeout=25)
                rr = _zai_vision({"thinking": {"type": "disabled"}})
                if rr.status_code == 400:  # some models reject the thinking flag — retry plain
                    rr = _zai_vision({})
                if rr.status_code == 200:
                    out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                    if out:
                        print(f"  [vision answered by: zai/{zvm}]")
                        if zvm != zai.ZAI_VISION_MODELS[0]:
                            zai.ZAI_VISION_MODELS.insert(0, zai.ZAI_VISION_MODELS.pop(zai.ZAI_VISION_MODELS.index(zvm)))
                        return out
                    continue
                reason = zai._zai_err(rr)
                if rr.status_code == 429:
                    if "1113" in reason or "balance" in reason.lower() or "package" in reason.lower():
                        print(f"  [zai vision {zvm}: not usable on this key — {reason}]")
                        if zvm in zai.ZAI_VISION_MODELS: zai.ZAI_VISION_MODELS.remove(zvm)
                        continue
                    zai._ZAI_VISION_WAIT["until"] = time.time() + 60  # genuine rate limit: cool down
                    print(f"  [zai vision rate-limited — cooling down 60s: {reason}]")
                    break
                if rr.status_code in (400, 404):
                    if zvm in zai.ZAI_VISION_MODELS: zai.ZAI_VISION_MODELS.remove(zvm)
                    print(f"  [zai vision {zvm}: unavailable on this key — {reason}]")
                    continue
                print(f"  [zai vision {zvm} failed — {reason}]")
            except Exception as e:
                print(f"  [zai vision failed: {str(e)[:80]}]")
    if config.MISTRAL_API_KEY and mistralai.MISTRAL_VISION_MODELS:
        for mvm in list(mistralai.MISTRAL_VISION_MODELS):
            if time.time() > deadline: return ""
            try:
                rr = HTTP.post(f"{config.MISTRAL_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {config.MISTRAL_API_KEY}", "Content-Type": "application/json"},
                    json={"model": mvm, "max_tokens": 400,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=20)
                if rr.status_code in (400, 404):
                    if mvm in mistralai.MISTRAL_VISION_MODELS: mistralai.MISTRAL_VISION_MODELS.remove(mvm)
                    print(f"  [mistral vision {mvm}: unavailable on this key — {rr.status_code}]")
                    continue
                if rr.status_code != 200: continue
                out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
                if out:
                    print(f"  [vision answered by: mistral/{mvm}]")
                    if mvm != mistralai.MISTRAL_VISION_MODELS[0]:
                        mistralai.MISTRAL_VISION_MODELS.insert(0, mistralai.MISTRAL_VISION_MODELS.pop(mistralai.MISTRAL_VISION_MODELS.index(mvm)))
                    return out
            except Exception as e:
                print(f"  [mistral vision failed: {str(e)[:60]}]")
    if gemini.client:
        for gm in list(gemini.GEMINI_VISION_MODELS):
            if time.time() > deadline: return ""
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
                    print(f"  [{gm}: retired — removing from list]")
                    if gm in gemini.GEMINI_VISION_MODELS: gemini.GEMINI_VISION_MODELS.remove(gm)
                elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    print(f"  [{gm}: quota limit — trying next]")
                else: print(f"  [{gm}: failed: {msg[:50]}]")
    if cohereai.COHERE_API_KEY and cohereai.COHERE_VISION_MODELS:
        for cvm in list(cohereai.COHERE_VISION_MODELS):
            if time.time() > deadline: return ""
            try:
                rr = HTTP.post(f"{cohereai.COHERE_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {cohereai.COHERE_API_KEY}"},
                    json={"model": cvm, "max_tokens": 250,
                          "messages": [{"role": "user", "content": [
                              {"type": "text", "text": instruction},
                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
                    timeout=20)
            except Exception as e:
                if cvm in cohereai.COHERE_VISION_MODELS and len(cohereai.COHERE_VISION_MODELS) > 1:
                    cohereai.COHERE_VISION_MODELS.remove(cvm)
                    print(f"  [cohere vision {cvm}: hung/timed out — removed from chain]")
                else:
                    print(f"  [cohere vision failed: {str(e)[:50]}]")
                continue
            if rr.status_code in (400, 404):
                if cvm in cohereai.COHERE_VISION_MODELS: cohereai.COHERE_VISION_MODELS.remove(cvm)
                print(f"  [cohere vision {cvm}: unavailable on this key — {rr.status_code}]")
                continue
            if rr.status_code != 200: continue
            out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
            if out:
                print(f"  [vision answered by: cohere/{cvm}]")
                if cvm != cohereai.COHERE_VISION_MODELS[0]:
                    cohereai.COHERE_VISION_MODELS.insert(0, cohereai.COHERE_VISION_MODELS.pop(cohereai.COHERE_VISION_MODELS.index(cvm)))
                return out
    for orm in list(openrouter.OR_VISION_MODELS):
        if time.time() > deadline: return ""
        try:
            rr = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {openrouter.OPENROUTER_API_KEY}"},
                json={"model": orm,
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": instruction},
                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                      "max_tokens": 400},
                timeout=20)
            if rr.status_code in (400, 404):
                if orm in openrouter.OR_VISION_MODELS: openrouter.OR_VISION_MODELS.remove(orm)
                continue
            if rr.status_code != 200: continue
            out = (((rr.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or "").strip()
            if out:
                print(f"  [vision answered by: {orm}]")
                if orm != openrouter.OR_VISION_MODELS[0]:
                    openrouter.OR_VISION_MODELS.insert(0, openrouter.OR_VISION_MODELS.pop(openrouter.OR_VISION_MODELS.index(orm)))
                return out
        except Exception: continue
    return ""

def _screenshot_text(instruction: str) -> str:
    _, raw, _scale = _vision_shot()
    return _vision_describe(raw, instruction)

def _find_element_xy(element: str):
    img, raw, scale = _vision_shot()
    w, h = img.size
    sw, sh = int(w / scale), int(h / scale)
    prompt = (
        f"This is a {sw}x{sh} Windows screenshot. Locate the clickable UI element, video thumbnail, or link for: '{element}'. "
        f"Ignore the small 'Tony' overlay window in the top-left corner. "
        f"If it is a YouTube video, return the center coordinates of the video thumbnail or video title link. "
        f"Return ONLY a valid JSON object with pixel coordinates in the {sw}x{sh} image: "
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
        except Exception: pass
    return _ocr_find_xy(element, img)
