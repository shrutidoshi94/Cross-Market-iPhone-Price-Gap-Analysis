"""
Streamlit Community Cloud entry point.

Deploy: https://share.streamlit.io
  - Main file path: streamlit_app.py
  - Python version: 3.13 (or 3.11+)
  - Secrets (App settings → Secrets), TOML:

      EXCHANGERATE_HOST_ACCESS_KEY = "..."
      PRICESAPI_KEY_1 = "..."
      PRICESAPI_KEY_2 = "..."
      PRICESAPI_KEY_3 = "..."
      PRICESAPI_KEY_4 = "..."
      PRICESAPI_KEY_5 = "..."

Note: named streamlit_app.py (not streamlit.py) so it does not shadow
the installed ``streamlit`` package.

Local:
  streamlit run streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path (Streamlit Cloud runs from repo root,
# but this keeps imports reliable if the working directory differs).
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import main

main()
