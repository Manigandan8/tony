# =====================================================================
# tony_core/providers/mistralai.py
# Provider module for Mistral AI.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - MISTRAL_BRAIN_MODELS: list of brain models (mutated on 400/404)
# - MISTRAL_VISION_MODELS: list of vision models (reordered or removed)
# =====================================================================

from tony_core import config

MISTRAL_API_KEY = config.MISTRAL_API_KEY
MISTRAL_BASE = config.MISTRAL_BASE
MISTRAL_BRAIN_MODELS = list(config.MISTRAL_BRAIN_MODELS)
MISTRAL_VISION_MODELS = list(config.MISTRAL_VISION_MODELS)
