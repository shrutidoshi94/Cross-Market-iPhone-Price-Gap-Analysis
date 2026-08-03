"""
Streamlit Community Cloud entry point.

Deploy at https://share.streamlit.io with:
  - Repository: your GitHub repo
  - Main file path: streamlit_app.py
  - Secrets: PRICESAPI_KEY_1..5 and EXCHANGERATE_HOST_ACCESS_KEY

Local equivalent:
  streamlit run streamlit_app.py

Note: this file is named streamlit_app.py (not streamlit.py) so it does not
shadow the installed ``streamlit`` package on import.
"""

from app import main

main()
