# =====================================================================
# tony_core/net.py
# Shared HTTP session and networking configuration.
# =====================================================================

import requests

# ---------- SAFE ENV & PERSISTENT HTTP SESSION ----------
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "TonyDesktopAgent/2.0"})
