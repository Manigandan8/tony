# =====================================================================
# tony_core/providers/cohereai.py
# Provider module for Cohere.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - COHERE_BRAIN_MODELS: list of brain models (mutated on 400/404)
# - COHERE_VISION_MODELS: list of vision models (mutated if hangs or 400/404)
# - COHERE_MODEL: active brain model ID
# =====================================================================

from tony_core import config

COHERE_API_KEY = config.COHERE_API_KEY
COHERE_BASE = config.COHERE_BASE
COHERE_BRAIN_MODELS = list(config.COHERE_BRAIN_MODELS)
COHERE_VISION_MODELS = list(config.COHERE_VISION_MODELS)
COHERE_MODEL = config.COHERE_MODEL
