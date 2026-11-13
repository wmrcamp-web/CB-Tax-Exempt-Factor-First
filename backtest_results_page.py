"""Streamlit Backtest Results view with explicit, session-backed reconciled rebuilds."""

from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st

import backtest_exports as exports
from data_errors import MarketDataError
from ui_style import apply_nav_style
from access_gate import require_access


def render_results(data_dir=None, provider=None):
    st.set_page_config(page_title="Backtest Results", layout="wide")
    apply_nav_style()
    require_access()
    st.title("Historical Backtest Results")
    sharadar = False
    if provider is None and data_dir is None:
        source_choice = st.sidebar.radio(
            "Backtest data source", ["Existing / live-provider results", "Sharadar historical results"],
        )
        if source_choice == "Sharadar historical results":
            from sharadar_backtest import DATA_DIR, configured_provider
            from streamlit.errors import StreamlitSecretNotFoundError

            try:
                settings = st.secrets.to_dict()
            except StreamlitSecretNotFoundError:
                settings = {}
            try:
                estimate = st.sidebar.checkbox(
                    "Estimate delisted exits at last close",
                    help="Confirmed delisted stocks only: use last adjusted close and hold "
                         "cash at 0%. Actual proceeds are unverified; bankruptcy returns "
                         "can be overstated. Estimates are labeled in downloads.",
                )
                provider = configured_provider(
                    settings, delisting_policy="last_close_cash" if estimate else "strict",
                )
            except MarketDataError as exc:
                st.error(str(exc))
                return
            data_dir = DATA_DIR
            sharadar = True
            st.warning(
                "Sharadar results are stored separately. Historical membership and market "
                "caps are used, but current Sharadar sector labels can introduce "
                "classification/look-ahead bias. Optional last-close exit estimates are "
                "not verified merger or bankruptcy payouts. DISCK, NWS, TFCFA and UA are excluded from "
                "selection at the user's request; the historical universe is altered."
            )
    st.markdown("Review the annual and cumulative performance of the Still Point Protocol against the S&P 500 benchmark.")
    st.caption(
        "Saved backtests retain the protocol rules used when they were generated; "
        "they are not automatically recalculated when selection rules change. "
        "New calculations use the current same-window SPY gate and 0.59 non-shadow cutoff."
    )
    data_dir = exports.DATA_DIR if data_dir is None else Path(data_dir)
    summary_path = data_dir / "backtest_results.csv"
    key = f"backtest_details:{summary_path.resolve()}"
    state = st.session_state.setdefault(key, {})
    if sharadar:
        generate_sharadar_results(data_dir, provider, state)
        for year, error in state.get("generation_failures", {}).items():
            st.error(f"{year}: Not published: {error}")
    try:
        source, df = exports.read_summary(summary_path)
    except FileNotFoundError:
        df = state.get("summary", pd.DataFrame(columns=exports.SUMMARY_COLUMNS))
        source = df.to_csv(index=False).encode("utf-8")
        if df.empty:
            if sharadar:
                st.info("No results have been generated yet. Choose the years above and click "
                        "**Generate Sharadar backtest**.")
            else:
                st.warning("No valid annual summary is available.")
            return
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        st.warning(f"No valid annual summary is available: {exc}")
        return
    if df.empty:
        st.warning("No valid completed annual summary years are available.")
        return

    summary_section = st.container()
    st.markdown("### Detailed Yearly Holdings")
    st.caption(
        "Each published year's annual summary row and holdings rows come from one yearly "
        "calculation: selection, holding-year prices, ticker returns, equal-weight portfolio "
        "return, benchmark return and alpha are computed once. A year is published only if "
        "ticker returns match their start/end prices and the weighted holdings return matches "
        "the summary portfolio return. Rebuilding reruns selection using the current provider's "
        "constituents and market caps (not original historical holdings) and replaces that "
        "year's summary row; replaced rows are archived. Equal weights are unchanged. "
        "Legacy providers fill weekly closes; Sharadar validates annual endpoint prices "
        "or explicitly labels opt-in last-close cash estimates. Weights and returns are decimal fractions "
        "(0.10 = 10%); Weight_Percent uses percent units.")
    details_path = data_dir / "backtest_detailed_results.csv"
    disk, problem = exports.read_details(details_path)
    if problem:
        st.warning(problem)
    df = exports.merge_summary(df, state.get("summary"))
    details = exports.merge_details(
        disk, state.get("details", exports.validate_details(pd.DataFrame())))
    published, mismatches = exports.reconcile_details(df, details)
    summary_years = set(df["Year"])
    missing = sorted(summary_years - set(published["Year"]))
    if missing and st.button("Show Holdings Details for Prior Years",
                             type="primary"):
        progress = st.progress(0.0, text="Starting current-provider rebuild...")

        def report(done, total, year):
            progress.progress(done / total, text=f"Rebuilding {year}: {done}/{total} years processed")

        with st.spinner("Rebuilding summary and holdings; this may take several minutes..."):
            rebuilt, generated, failures = exports.rebuild_years(
                missing, provider=provider, progress=report)
        previous = state.get("summary")
        state["summary"] = rebuilt if previous is None else exports.merge_summary(previous, rebuilt)
        state["details"] = exports.merge_details(
            state.get("details", exports.validate_details(pd.DataFrame())), generated)
        state["failures"] = failures
        if not rebuilt.empty:
            try:
                exports.persist_summary(rebuilt, summary_path)
                exports.persist_details(generated, details_path)
                state.pop("persistence_error", None)
                state.pop("summary", None)
                source, _ = exports.read_summary(summary_path)
            except OSError as exc:
                state["persistence_error"] = str(exc)
        df = exports.merge_summary(df, rebuilt)
        details = exports.merge_details(details, generated)
        published, mismatches = exports.reconcile_details(df, details)
        missing = sorted(summary_years - set(published["Year"]))
    if state.get("persistence_error"):
        st.warning(
            "Local persistence is unavailable. Rebuilt summary and holdings remain downloadable in "
            "this session across reruns, but will be lost when the session ends. "
            f"Storage error: {state['persistence_error']}")
    for year, error in state.get("failures", {}).items():
        if year in missing:
            st.warning(f"{year}: Rebuild failed: {error}. Check provider availability/data and "
                       "retry. Successful years are retained.")
    for year, error in mismatches.items():
        if year in missing and year not in state.get("failures", {}):
            st.warning(f"{year}: Detailed holdings do not reconcile with the annual summary "
                       f"({error}); they are not published. Rebuild to regenerate both together.")
    if missing:
        st.info(f"Reconciled detailed holdings are missing for: {', '.join(map(str, missing))}.")
    if not published.empty:
        if missing:
            st.warning(f"Detailed holdings cover {published['Year'].nunique()} of {len(summary_years)} "
                       "summary years; the download below is partial.")
            label = "Download partial detailed holdings CSV for Google Sheets"
        else:
            st.success("Detailed holdings reconcile with the annual summary for all displayed "
                       "completed years.")
            label = "Download detailed holdings CSV for Google Sheets"
        st.download_button(label, data=published.to_csv(index=False).encode("utf-8"),
                           file_name="backtest_detailed_results.csv", mime="text/csv")
        st.dataframe(published, use_container_width=True)

    with summary_section:
        render_summary(
            source, df, state.get("summary") is not None, sharadar=sharadar,
            incomplete=bool(state.get("generation_failures")),
        )


def generate_sharadar_results(data_dir, provider, state):
    st.caption("Generate results directly from Sharadar; no manual sector files are required "
               "with the default sector source. Replaced years are archived.")
    last_year = date.today().year - 1
    first = st.number_input("First portfolio year", min_value=1999, max_value=last_year,
                            value=max(1999, last_year - 9), step=1)
    last = st.number_input("Last portfolio year", min_value=1999, max_value=last_year,
                           value=last_year, step=1)
    if first > last:
        st.error("First portfolio year must not follow last portfolio year.")
        return
    if not st.button("Generate Sharadar backtest", type="primary"):
        return
    progress = st.progress(0.0, text="Starting Sharadar backtest...")

    def report(done, total, year):
        progress.progress(done / total, text=f"Processing {year}: {done}/{total} years")

    with st.spinner("Fetching historical Sharadar data and calculating portfolios..."):
        rebuilt, generated, failures = exports.rebuild_years(
            range(int(first), int(last) + 1), provider=provider, progress=report,
        )
    state["generation_failures"] = failures
    if rebuilt.empty:
        st.error("No years were published. See the year-specific errors below.")
        return
    previous = state.get("summary")
    state["summary"] = rebuilt if previous is None else exports.merge_summary(previous, rebuilt)
    state["details"] = exports.merge_details(
        state.get("details", exports.validate_details(pd.DataFrame())), generated,
    )
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        exports.persist_summary(rebuilt, data_dir / "backtest_results.csv")
        exports.persist_details(generated, data_dir / "backtest_detailed_results.csv")
        state.pop("summary", None)
        state.pop("details", None)
        state.pop("persistence_error", None)
    except OSError as exc:
        state["persistence_error"] = str(exc)
        st.warning(f"Results are session-only because local storage failed: {exc}")
    st.success(f"Published {len(rebuilt)} of {int(last) - int(first) + 1} requested years.")


def render_summary(source, df, session_only, sharadar=False, incomplete=False):
    if session_only:
        source = df.to_csv(index=False).encode("utf-8")
    beat_count = int((df["Portfolio_Return"] > df["Benchmark_Return"]).sum())
    start_year, end_year = int(df["Year"].min()), int(df["Year"].max())
    st.info(
        f"In the displayed {start_year}–{end_year} backtest, the portfolio beat the "
        f"S&P 500 in {beat_count} of {len(df)} annual periods. For context, SPIVA reported "
        "that 84.3% of U.S. large-cap active funds underperformed the S&P 500 over the "
        "decade ending in 2024. This is not a direct comparison: the periods and methods "
        "differ. " +
        ("These isolated Sharadar results use dated membership and market caps; sector source "
         "and exit policies are recorded in the exports. Incomplete years can be absent. " if sharadar else
         "This backtest uses today’s constituents and market-cap rankings for historical years. ") +
        "It does not show how likely the protocol is to beat active managers."
    )
    st.download_button(
        "Download annual summary CSV", data=source,
        file_name="backtest_results.csv", mime="text/csv")
    st.caption("Annual-summary file (including any source rows excluded from this completed-year "
               "view). This is not a detailed holdings export." if not session_only else
               "Session-only annual summary including rebuilt years (local persistence is unavailable).")
    df = df.copy()
    if sharadar:
        if "Sector_Source" in df:
            st.caption("Sector sources in saved results: " + ", ".join(df["Sector_Source"].dropna().unique()))
        if "Estimated_Exit_Count" in df:
            estimates = pd.to_numeric(df["Estimated_Exit_Count"], errors="coerce").fillna(0).sum()
            if estimates:
                st.warning(f"Saved results include {int(estimates)} estimated delisted exits. "
                           "See Exit_Valuation in detailed holdings; actual payouts are unverified.")
    if sharadar and (incomplete or df["Year"].diff().dropna().ne(1).any()):
        st.warning(
            "Sharadar results have gaps in annual coverage. Cumulative performance and CAGR "
            "are withheld rather than treating skipped years as consecutive holding periods."
        )
        st.dataframe(df, use_container_width=True)
        return
    df["Cumulative_Portfolio"] = (1 + df["Portfolio_Return"]).cumprod() - 1
    df["Cumulative_Benchmark"] = (1 + df["Benchmark_Return"]).cumprod() - 1
    years = len(df)
    port_cagr = (1 + df["Cumulative_Portfolio"].iloc[-1]) ** (1 / years) - 1
    bench_cagr = (1 + df["Cumulative_Benchmark"].iloc[-1]) ** (1 / years) - 1
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Annualized Compound Return (CAGR)", f"{port_cagr*100:.2f}%")
    c2.metric("Benchmark CAGR", f"{bench_cagr*100:.2f}%")
    c3.metric("Total Cumulative Portfolio Return", f"{df['Cumulative_Portfolio'].iloc[-1]*100:.2f}%")
    c4.metric("Total Cumulative Benchmark Return", f"{df['Cumulative_Benchmark'].iloc[-1]*100:.2f}%")
    st.markdown("### Cumulative Performance Chart")
    st.line_chart(df.set_index("Year")[["Cumulative_Portfolio", "Cumulative_Benchmark"]])
    st.markdown("### Annual Summary Returns")
    formats = {column: "{:.2%}" for column in (
        "Portfolio_Return", "Benchmark_Return", "Alpha",
        "Cumulative_Portfolio", "Cumulative_Benchmark") if column in df}
    st.dataframe(df.style.format(formats), use_container_width=True)
