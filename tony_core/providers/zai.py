# =====================================================================
# tony_core/providers/zai.py
# Provider module for Z.ai (GLM).
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - ZAI_BRAIN_MODELS: list of brain models (mutated on 400/404/1211)
# - ZAI_VISION_MODELS: list of vision models (reordered or items removed)
# - ZAI_BRAIN_MODEL: active brain model ID (rebound when model succeeds)
# - _ZAI_EP: dict {"idx": 0} chosen at boot by _zai_probe()
# - _ZAI_VISION_WAIT: dict {"until": 0.0} cooldown after 429 rate limit
# =====================================================================

from tony_core import config
from tony_core.net import HTTP

ZAI_API_KEY = config.ZAI_API_KEY
ZAI_BASES = list(config.ZAI_BASES)
ZAI_AS_PRIMARY = config.ZAI_AS_PRIMARY

ZAI_BRAIN_MODELS = list(config.ZAI_BRAIN_MODELS)
ZAI_VISION_MODELS = list(config.ZAI_VISION_MODELS)
ZAI_BRAIN_MODEL = ZAI_BRAIN_MODELS[0]

_ZAI_EP = {"idx": 0}               # chosen once at boot by _zai_probe()
_ZAI_VISION_WAIT = {"until": 0.0}  # cooldown after a genuine rate-limit 429

def _zai_headers():
    return {"Authorization": f"Bearer {ZAI_API_KEY}", "Content-Type": "application/json",
            "Accept-Language": "en-US,en"}

def _zai_base():
    return ZAI_BASES[min(_ZAI_EP["idx"], len(ZAI_BASES) - 1)]

def _zai_err(r) -> str:
    """Human-readable Z.ai failure, e.g. 'HTTP 429 [1113]: Insufficient balance...'"""
    code, message = "", ""
    try:
        err = r.json().get("error", {}) or {}
        code = str(err.get("code", "") or "")
        message = (err.get("message", "") or "").strip()
    except Exception:
        message = (r.text or "")[:150].strip()
    return f"HTTP {r.status_code} [{code or '?'}]: {message[:130]}"

def _zai_probe():
    """Boot-time check: does this key work on the standard API, or on the coding endpoint?"""
    if not ZAI_API_KEY: return
    payload = {"model": ZAI_BRAIN_MODELS[0], "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    for i, base in enumerate(ZAI_BASES):
        try:
            r = HTTP.post(f"{base}/chat/completions", headers=_zai_headers(), json=payload, timeout=6)
            if r.status_code == 200:
                _ZAI_EP["idx"] = i
                print(f"[zai] endpoint ready: {base}")
                return
            print(f"[zai] probe {base} -> {_zai_err(r)}")
        except Exception as e:
            print(f"[zai] probe {base} failed: {str(e)[:60]}")
    print("[zai] WARNING: no endpoint accepted this key — run zai_test.py to debug.")
    print("[zai] (Tony will still retry at runtime; other brains/vision will cover meanwhile)")
