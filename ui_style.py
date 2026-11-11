import streamlit as st

# CSS override for the auto-generated multipage sidebar navigation:
# larger, bold, ALL-CAPS labels for the page links (app / Backtest Results / How It Works).
_NAV_CSS = """
<style>
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNav"] a span {
    font-size: 1rem !important;
    font-weight: 600 !important;
    text-transform: none !important;
}
</style>
"""


def apply_nav_style():
    """Inject the sidebar navigation styling. Call after st.set_page_config on each page."""
    st.markdown(_NAV_CSS, unsafe_allow_html=True)
