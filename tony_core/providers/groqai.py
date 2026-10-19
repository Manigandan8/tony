# =====================================================================
# tony_core/providers/groqai.py
# Provider module for Groq.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - groq: Groq client instance
# - AVAILABLE_MODELS: set of discovered model IDs from Groq
# - GROQ_VISION_MODEL: active vision model (or None; rebound on error)
# - BRAIN_MODEL: active groq brain model ID
# =====================================================================

from groq import Groq
from tony_core import config

groq = Groq(api_key=config.GROQ_API_KEY)

AVAILABLE_MODELS = set()
GROQ_VISION_MODEL = None
BRAIN_MODEL = "openai/gpt-oss-120b"

def _groq_model_set():
    try:
        resp = groq.models.list()
        data = getattr(resp, "data", resp)
        available = set()
        for m in data:
            if isinstance(m, str): available.add(m)
            elif isinstance(m, (tuple, list)): available.add(m[0])
            else:
                mid = getattr(m, "id", None)
                if mid: available.add(mid)
        return available
    except Exception:
        return set()

def pick_vision_model():
    hints = ("vision", "scout", "maverick", "llama-4")
    for m in sorted(AVAILABLE_MODELS):
        if any(h in m.lower() for h in hints):
            if not any(x in m.lower() for x in ("tts", "guard", "embed", "safeguard")):
                return m
    return None

def pick_groq_model():
    prefs = ["openai/gpt-oss-120b", "gpt-oss-120b", "llama-3.3-70b-versatile",
             "llama-3.1-8b-instant", "openai/gpt-oss-20b", "gemma2-9b-it"]
    for p in prefs:
        if p in AVAILABLE_MODELS: return p
    for a in sorted(AVAILABLE_MODELS):
        if not any(x in a.lower() for x in ("whisper", "tts", "guard", "embed", "playai", "scout", "maverick", "vision")):
            return a
    return prefs[0]

def init_models():
    global AVAILABLE_MODELS, GROQ_VISION_MODEL, BRAIN_MODEL
    AVAILABLE_MODELS = _groq_model_set()
    GROQ_VISION_MODEL = pick_vision_model()
    print(f"[vision] groq: {GROQ_VISION_MODEL or 'none (using Gemini/OpenRouter VLM -> OCR)'}")
    BRAIN_MODEL = pick_groq_model()
    print(f"[brain] using: {BRAIN_MODEL}\n")
