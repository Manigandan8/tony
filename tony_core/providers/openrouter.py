# =====================================================================
# tony_core/providers/openrouter.py
# Provider module for OpenRouter.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - _BAD_OR_SUBSTRINGS: tuple of blacklisted substrings
# - OR_VISION_MODELS: list of vision models (reordered or items removed)
# - OR_BRAIN_MODELS: list of brain models (items removed on error)
# =====================================================================

from tony_core import config
from tony_core.net import HTTP

OPENROUTER_API_KEY = config.OPENROUTER_API_KEY

_BAD_OR_SUBSTRINGS = ("safety", "guard", "moderation", "dots-", "inkling", "embed", "tts", "audio")
OR_VISION_MODELS = []
OR_BRAIN_MODELS = []

def _or_models():
    if not OPENROUTER_API_KEY: return None
    try:
        r = HTTP.get("https://openrouter.ai/api/v1/models", timeout=10)
        return r.json().get("data", [])
    except Exception as e:
        print(f"  [openrouter discovery failed: {str(e)[:50]}]")
        return None

def _rank(mid):
    low = mid.lower()
    for i, fam in enumerate(("qwen", "gemma", "nemotron", "mistral", "llama", "gpt", "glm", "kimi")):
        if fam in low: return i
    return 99

def pick_openrouter_vision(all_models):
    if not all_models: return []
    out = []
    for m in all_models:
        mid = m.get("id", "")
        if any(bad in mid.lower() for bad in _BAD_OR_SUBSTRINGS): continue
        pricing = m.get("pricing", {})
        mods = (m.get("architecture", {}) or {}).get("input_modalities", []) or []
        if str(pricing.get("prompt", "1")) == "0" and "image" in mods:
            out.append(mid)
    out.sort(key=_rank)
    return out[:5]

def pick_openrouter_brains(all_models):
    if not all_models: return []
    cands = []
    for m in all_models:
        mid = m.get("id", "")
        if any(bad in mid.lower() for bad in _BAD_OR_SUBSTRINGS): continue
        pricing = m.get("pricing", {})
        params = m.get("supported_parameters", []) or []
        mods = (m.get("architecture", {}) or {}).get("input_modalities", []) or []
        if str(pricing.get("prompt", "1")) == "0" and "tools" in params and "text" in mods:
            cands.append(mid)
    def r2(mid):
        low = mid.lower()
        for i, fam in enumerate(("nemotron", "qwen", "glm", "deepseek", "mistral", "llama", "gemma")):
            if fam in low: return i
        return 99
    cands.sort(key=r2)
    return cands[:3]

def init_models():
    global OR_VISION_MODELS, OR_BRAIN_MODELS
    all_or = _or_models()
    OR_VISION_MODELS = pick_openrouter_vision(all_or)
    OR_BRAIN_MODELS = pick_openrouter_brains(all_or)
    print(f"[vision-or] models: {OR_VISION_MODELS or 'none (check OPENROUTER_API_KEY)'}")
    print(f"[brain-or] models: {OR_BRAIN_MODELS or 'none'}")
