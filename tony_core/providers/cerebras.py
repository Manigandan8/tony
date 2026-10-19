# =====================================================================
# tony_core/providers/cerebras.py
# Provider module for Cerebras.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - CEREBRAS_BRAIN_MODELS: list of brain models (mutated on 400/404)
# =====================================================================

from tony_core import config

CEREBRAS_API_KEY = config.CEREBRAS_API_KEY
CEREBRAS_BASE = config.CEREBRAS_BASE
CEREBRAS_BRAIN_MODELS = list(config.CEREBRAS_BRAIN_MODELS)
