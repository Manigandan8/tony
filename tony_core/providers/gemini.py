# =====================================================================
# tony_core/providers/gemini.py
# Provider module for Google Gemini.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - client: genai.Client instance or None
# - GEMINI_BRAIN_MODEL: active brain model ID
# - GEMINI_VISION_MODELS: list of vision model IDs (mutated on 404/retirement)
# =====================================================================

from google import genai
from google.genai import types
from tony_core import config

client = genai.Client(
    api_key=config.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=15 * 1000)
) if config.GEMINI_API_KEY else None

GEMINI_VISION_MODELS = list(config.GEMINI_VISION_MODELS)
GEMINI_BRAIN_MODEL = config.GEMINI_BRAIN_MODEL
