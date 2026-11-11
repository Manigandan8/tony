# =====================================================================
# tony_core/providers/groqai.py
# Provider module for Groq (with max_retries=0 for instant model rotation).
# =====================================================================

from groq import Groq
from tony_core import config

# CRITICAL: max_retries=0 stops the Groq SDK from silently sleeping 30-60s on 429 TPM limits!
groq = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=10.0)

AVAILABLE_MODELS = set()
GROQ_VISION_MODEL = None
BRAIN_MODEL = "llama-3.3-70b-versatile"
CHAT_MODEL = "llama-3.1-8b-instant"
GROQ_MODEL_POOL = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "gemma2-9b-it",
]


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


def pick_vision_model():
    hints = ("vision", "scout", "maverick", "llama-4")
    for m in sorted(AVAILABLE_MODELS):
        if any(h in m.lower() for h in hints):
            if not any(x in m.lower() for x in ("tts", "guard", "embed", "safeguard")):
                return m
    return None


def pick_groq_model():
    for p in GROQ_MODEL_POOL:
        if p in AVAILABLE_MODELS:
            return p
    for a in sorted(AVAILABLE_MODELS):
        if not any(x in a.lower() for x in ("whisper", "tts", "guard", "embed", "playai", "scout", "maverick", "vision")):
            return a
    return GROQ_MODEL_POOL[0]


def pick_chat_model():
    prefs = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b"]
    for p in prefs:
        if p in AVAILABLE_MODELS:
            return p
    return pick_groq_model()


def init_models():
    global AVAILABLE_MODELS, GROQ_VISION_MODEL, BRAIN_MODEL, CHAT_MODEL
    AVAILABLE_MODELS = _groq_model_set()
    GROQ_VISION_MODEL = pick_vision_model()
    print(f"[vision] groq: {GROQ_VISION_MODEL or 'none (using Gemini/OpenRouter VLM -> OCR)'}")
    BRAIN_MODEL = pick_groq_model()
    CHAT_MODEL = pick_chat_model()
    print(f"[brain] using: {BRAIN_MODEL} (chat: {CHAT_MODEL})\n")