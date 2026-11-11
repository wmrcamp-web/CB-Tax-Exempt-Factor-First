import streamlit as st
import io
import requests
import datetime
import concurrent.futures
from portfolio_pipeline import run_pipeline as select_portfolio, BANNED_TICKERS_SECTORS
from data_provider import DEFAULT_PROVIDER
from dashboard_controls import market_data_refresh_control, performance_gate_controls, run_protocol
from ui_style import apply_nav_style
from access_gate import legal_footer, require_access


# ---------------------------------------------------------
# Page Configuration
# ---------------------------------------------------------
st.set_page_config(
    page_title="CB Tax Exempt Factor First Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)
apply_nav_style()
require_access()

st.title("CB Tax Exempt Factor First")
st.markdown("Automated Selection Gates: SP5OO Market Cap, Alpha Return, Varimax, Shadow List Priority, Factor Diversification")

# ---------------------------------------------------------
# Sidebar Controls
# ---------------------------------------------------------
st.sidebar.header("Protocol Configuration")

as_of_date = st.sidebar.date_input("As of Date (Trailing End Period)", value=datetime.date.today())

# Default gate: SPY T12M computed in the run from the same prices and window as stocks.
min_return_threshold = performance_gate_controls()
market_data_refresh_control()

universe_size = st.sidebar.number_input("Universe Size (Top S&P by Market Cap)", min_value=50, max_value=250, value=150, step=5)
n_factors = st.sidebar.number_input("Varimax Latent Factors", min_value=3, max_value=12, value=8, step=1)
shadow_cutoff = st.sidebar.slider("Shadow List Max Loading Cutoff", min_value=0.20, max_value=0.60, value=0.40, step=0.05)
target_portfolio_size = st.sidebar.number_input("Target Portfolio Size", min_value=10, max_value=30, value=16, step=1)

st.sidebar.markdown("---")
st.sidebar.markdown("**Constraints Enforced:**\n- No Oil\n- No Gold\n- No Bitcoin\n- Equal Weight Allocation (1/N)\n- Exclude GOOG (Duplicate share class)\n- Max 3 stocks per Dominant Factor\n- Min 0.59 absolute loading for non-shadow selections")

# ---------------------------------------------------------
# Quantitative Calculation Helpers
# ---------------------------------------------------------
# Hurst calculation removed as per request.


# ---------------------------------------------------------
# Sector Constraint Screening
# ---------------------------------------------------------

# ---------------------------------------------------------
# Data Engine & Execution
# ---------------------------------------------------------


def run_pipeline(end_date, gate_threshold, provider=None, trace=None):
    return select_portfolio(
        end_date, gate_threshold, provider=provider, trace=trace,
        universe_size=universe_size, n_factors=n_factors,
        shadow_cutoff=shadow_cutoff, target_portfolio_size=target_portfolio_size,
        ui=st,
    )

# ---------------------------------------------------------
# Execution Trigger & Layout
# ---------------------------------------------------------
run_sidebar = st.sidebar.button("Run Protocol Screen", type="primary")
run_main = st.button("Run Protocol", type="primary")

run_requested = run_sidebar or run_main
result = run_protocol(run_pipeline, as_of_date, min_return_threshold)[0] if run_requested else None
if result is not None:
    sp500_ret, metrics_df, loadings_df, portfolio_df, survivors_df, factor_headers = result

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("S&P 500 T12M Return", f"{sp500_ret*100:.2f}%")
    col2.metric("Screen Survivors", len(metrics_df))
    if not portfolio_df.empty:
        col3.metric("Shadow List Count", portfolio_df["In_Shadow_List"].sum())
        col4.metric("Final Portfolio Holdings", len(portfolio_df))

        st.subheader(f"Final Portfolio Allocation (As of {as_of_date})")
        st.dataframe(
            portfolio_df[["Weight (%)", "In_Shadow_List", "Dominant_Factor", "Downside_Beta", "Max_Factor_Loading", "Sector", "T12M_Return"]]
            .style.format({
                "Weight (%)": "{:.2f}%",
                "Downside_Beta": "{:.3f}",
                "Max_Factor_Loading": "{:.3f}",
                "T12M_Return": "{:.2%}"
            }),
            use_container_width=True
        )

        avg_port_ret = portfolio_df["T12M_Return"].mean()
        st.metric("Portfolio Average Period Return", f"{avg_port_ret*100:.2f}%")

        csv = portfolio_df.to_csv().encode("utf-8")
        st.download_button(
            label="Download Portfolio Allocation CSV",
            data=csv,
            file_name=f"still_point_portfolio_{as_of_date}.csv",
            mime="text/csv"
        )

        st.markdown("---")
        st.subheader("Varimax Factor Loadings Matrix (Survivors)")
        st.dataframe(loadings_df.style.background_gradient(cmap="Blues"), use_container_width=True)

        st.markdown("---")
        st.subheader("Surviving Pool (Ranked by Market Cap)")
        st.dataframe(
            survivors_df.style.format({
                "T12M_Return": "{:.2%}",
                "Market_Cap": "${:,.0f}",
                "Downside_Beta": "{:.3f}"
            }),
            use_container_width=True
        )

        avg_surv_ret = survivors_df["T12M_Return"].mean()
        med_surv_ret = survivors_df["T12M_Return"].median()
        mcol1, mcol2 = st.columns(2)
        mcol1.metric("Average Surviving Period Return", f"{avg_surv_ret*100:.2f}%")
        mcol2.metric("Median Surviving Period Return", f"{med_surv_ret*100:.2f}%")
    else:
        st.warning("No portfolio was generated based on the current criteria.")
elif not run_requested:
    st.info("Adjust the parameters in the sidebar and click **Run Protocol** to compute the portfolio.")

legal_footer()
