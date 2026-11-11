"""Sidebar gate/refresh controls and run reporting shared by both dashboards."""

import streamlit as st

import data_fetcher
from data_errors import MarketDataError
from portfolio_pipeline import BENCHMARK_GATE
from selection_trace import SelectionTrace


GATE_SOURCE_BENCHMARK = "S&P 500 (SPY) trailing return (default)"
GATE_SOURCE_CUSTOM = "Custom threshold"


def performance_gate_controls():
    """Return BENCHMARK_GATE (default), a custom decimal threshold, or None (gate off)."""
    enforce = st.sidebar.toggle("Enforce Minimum Trailing Return Gate", value=True)
    if not enforce:
        st.sidebar.caption("Gate off: no minimum trailing return is applied.")
        return None
    source = st.sidebar.radio(
        "Gate threshold source", [GATE_SOURCE_BENCHMARK, GATE_SOURCE_CUSTOM],
        help="The default uses SPY's return over exactly the same weeks as each stock.",
    )
    if source == GATE_SOURCE_BENCHMARK:
        st.sidebar.caption(
            "Calculated during each run from the same weekly closes, fill rules and "
            "53-close trailing window (ending at the As of Date) used for every stock's "
            "T12M return. The applied value and window are shown with the results."
        )
        return BENCHMARK_GATE
    return st.sidebar.number_input(
        "Custom Minimum Trailing Return Threshold (%)",
        value=0.0, step=1.0, format="%.2f",
        help="Applied exactly as entered instead of the SPY trailing return.",
    ) / 100.0


def market_data_refresh_control():
    if st.sidebar.button(
        "Refresh market data",
        help="Clear cached S&P 500 constituents, market caps and SPY benchmark returns "
             "so the next run refetches them. Access and identity state are not affected.",
    ):
        data_fetcher.clear_market_data_caches()
        st.sidebar.success("Market-data caches cleared. Run the protocol to refetch.")


def describe_run(trace):
    window = f"weekly closes {trace.benchmark_window_start} to {trace.benchmark_window_end}"
    spy = trace.benchmark_return
    spy_text = f"{spy:.2%}" if spy is not None else "unavailable"
    if trace.gate_mode == "benchmark":
        gate = (f"T12M return ≥ {trace.gate_threshold:.2%}, the S&P 500 (SPY) trailing "
                f"return from this run's {window}")
    elif trace.gate_mode == "fixed":
        gate = (f"T12M return ≥ {trace.gate_threshold:.2%} (custom threshold); "
                f"SPY returned {spy_text} over the same {window}")
    else:
        gate = f"off; SPY returned {spy_text} over the same {window}"
    return (
        f"**As of {trace.rebalance_date}.** Applied performance gate: {gate}.  \n"
        f"Market caps: {len(trace.screened_universe)} screened candidates ranked "
        f"with unavailable caps assigned zero and ranked last; "
        f"universe = top {len(trace.market_cap_universe)} by cap."
    )


def run_protocol(run_pipeline, as_of_date, gate_threshold):
    """Run selection; show market-data failures as an error instead of a traceback.

    Returns (result, trace); result is None when market data was unavailable.
    """
    trace = SelectionTrace()
    try:
        result = run_pipeline(as_of_date, gate_threshold, trace=trace)
    except MarketDataError as exc:
        st.error(
            f"No portfolio was generated: {exc}  \n"
            "Click **Refresh market data** in the sidebar and run again. If the problem "
            "persists, the data source may be unavailable or rate limiting requests."
        )
        return None, trace
    st.info(describe_run(trace))
    return result, trace
