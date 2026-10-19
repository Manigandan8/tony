# =====================================================================
# tony_core/providers/github_models.py
# Provider module for GitHub Models.
#
# SHARED MUTABLE GLOBALS OWNED BY THIS MODULE:
# - GITHUB_BRAIN_MODELS: list of brain models (mutated on 400/404)
# - GITHUB_VISION_MODELS: list of vision models (reordered or removed)
# =====================================================================

from tony_core import config

GITHUB_TOKEN = config.GITHUB_TOKEN
GITHUB_BASE = config.GITHUB_BASE
GITHUB_BRAIN_MODELS = list(config.GITHUB_BRAIN_MODELS)
GITHUB_VISION_MODELS = list(config.GITHUB_VISION_MODELS)
