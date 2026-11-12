"""Selection orchestration shared by the dashboard and headless backtests."""

from contextlib import nullcontext
import datetime
import math
import time

import pandas as pd

import protocol_engine as engine
from data_errors import BenchmarkError, MarketDataError, describe_error
from data_provider import DEFAULT_PROVIDER
from symbol_utils import normalize_symbol


# Default performance gate: the SPY trailing-12-month return computed from the same
# weekly price download, fill rules and 53-close window as each stock's return.
BENCHMARK_GATE = "spy_t12m"


BANNED_TICKERS_SECTORS = {
    "XLE", "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC", "PSX", "VLO",
    "GLD", "IAU", "NEM", "GOLD", "AEM",
    "BITO", "IBIT", "GBTC", "MSTR", "COIN", "GOOG",
}


def gate_mode(gate_threshold):
    """Classify a gate argument: BENCHMARK_GATE, None (off) or a finite number (fixed)."""
    if gate_threshold is None:
        return "off"
    if isinstance(gate_threshold, str):
        if gate_threshold == BENCHMARK_GATE:
            return "benchmark"
        raise ValueError(f"Unknown performance gate {gate_threshold!r}")
    try:
        finite = math.isfinite(float(gate_threshold))
    except (TypeError, ValueError):
        finite = False
    if not finite:
        raise BenchmarkError(
            f"Performance gate threshold {gate_threshold!r} is missing or not finite; "
            "the gate was not applied as 0% or disabled.")
    return "fixed"


def _acquire(trace, description, fetch, *args):
    """Run a provider call; record and raise failures as MarketDataError."""
    try:
        return fetch(*args)
    except MarketDataError as exc:
        if trace is not None:
            trace.fail(exc)
        raise
    except Exception as exc:
        error = MarketDataError(f"{description} failed ({describe_error(exc)}).")
        if trace is not None:
            trace.fail(error)
        raise error from exc


def run_pipeline(end_date, gate_threshold, provider=None, trace=None, *,
                 universe_size=150, n_factors=8, shadow_cutoff=0.40,
                 target_portfolio_size=16, ui=None):
    """Select a portfolio as of end_date.

    gate_threshold: BENCHMARK_GATE (SPY T12M from this run's prices), a finite
    decimal return (fixed/custom threshold), or None (gate off). Missing or invalid
    market data raises MarketDataError after recording the failure on trace.
    """
    provider = DEFAULT_PROVIDER if provider is None else provider
    spinner = ui.spinner if ui is not None else lambda message: nullcontext()
    try:
        mode = gate_mode(gate_threshold)
    except (MarketDataError, ValueError) as exc:
        if trace is not None:
            trace.begin(end_date, None, provider, gate_mode="fixed")
            trace.fail(exc)
        raise
    if trace is not None:
        trace.begin(end_date, None if mode == "benchmark" else gate_threshold, provider,
                    gate_mode=mode)
    with spinner(f"Fetching S&P 500 constituents ending {end_date}..."):
        sp500_df = _acquire(trace, "S&P 500 constituent download",
                            provider.get_constituents, end_date)
        if trace is not None:
            trace.selection_notes.extend(getattr(provider, "notes", []))
        candidates, ticker_to_sector = engine.prepare_universe(
            sp500_df, BANNED_TICKERS_SECTORS)
        if trace is not None:
            trace.constituent_universe = sp500_df["Symbol"].map(normalize_symbol).tolist()
            trace.screened_universe = list(candidates)
    with spinner("Sorting universe by actual Market Capitalization..."):
        mcaps_dict = _acquire(trace, "Market-cap download",
                              provider.get_market_caps, candidates, end_date)
        mcaps_dict = _acquire(trace, "Market-cap normalization",
                              engine.validate_market_caps, candidates, mcaps_dict)
        unknown_caps = [t for t in candidates if mcaps_dict[t] == 0]
        if unknown_caps:
            warning = engine.market_cap_warning(unknown_caps)
            if trace is not None:
                trace.selection_notes.append(warning)
            if ui is not None:
                ui.warning(warning)
        all_tickers = _acquire(trace, "Market-cap ranking",
                               engine.select_universe, candidates, mcaps_dict, universe_size)
        if trace is not None:
            trace.market_cap_universe = [t for t in all_tickers if t != "SPY"]
    with spinner("Downloading historical price data..."):
        start_date = end_date - datetime.timedelta(days=engine.PRICE_HISTORY_DAYS)

        def download():
            for attempt in range(3):
                try:
                    return provider.get_weekly_closes(all_tickers, start_date, end_date)
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2 * (2 ** attempt))

        data = _acquire(trace, "Weekly price download", download)
        sp500_bench_return, t12m_returns, returns_df, bench_ret = _acquire(
            trace, "Trailing-return calculation", engine.prepare_returns, data)
        downside_returns, downside_bench = engine.prepare_downside_returns(data)
        if mode == "benchmark":
            gate_threshold = sp500_bench_return
        if trace is not None:
            trace.benchmark_return = float(sp500_bench_return)
            if mode == "benchmark":
                trace.gate_threshold = float(gate_threshold)
            start, end = engine.trailing_window_bounds(data)
            trace.benchmark_window_start = _label(start)
            trace.benchmark_window_end = _label(end)
            trace.price_eligible_tickers = [t for t in returns_df.columns if t != "SPY"]
    # FACTOR FIRST: factors are fitted on the full price-eligible universe, then the
    # performance gate is applied to the factor-eligible tickers.
    all_universe = [t for t in returns_df.columns if t != "SPY"]
    try:
        with spinner("Executing Phase 2: 8-Factor Varimax Orthogonality & Factor Loadings (full universe)..."):
            valid_all, loadings_df, max_loadings, dominant_factors = engine.fit_factors(
                returns_df, all_universe, n_factors)
    except engine.SelectionError as exc:
        if ui is not None:
            ui.error(str(exc))
        if trace is not None:
            trace.status = "selection_failed"
            trace.selection_notes.append(str(exc))
        return sp500_bench_return, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), []
    with spinner("Executing Phase 3: Performance Gate (T12M)..."):
        survivor_tickers, survivors_df = engine.screen_survivors(
            t12m_returns, downside_returns, downside_bench, mcaps_dict, gate_threshold)
        if trace is not None:
            trace.survivor_tickers = list(survivor_tickers)
        survivor_set = set(survivor_tickers)
        valid = [t for t in valid_all if t in survivor_set]
        loadings_df = loadings_df.loc[valid]
        if trace is not None:
            trace.factor_eligible_tickers = list(valid)
    try:
        if not valid:
            raise engine.SelectionError(
                "No factor-eligible tickers passed the performance gate!")
        with spinner("Executing Phase 4: Quantitative Scoring (Downside Beta) & Shadow List Identification..."):
            metrics_df, shadow_list = engine.score_metrics(
                valid, downside_returns, downside_bench, t12m_returns, max_loadings,
                dominant_factors, ticker_to_sector, shadow_cutoff)
    except engine.SelectionError as exc:
        if ui is not None:
            ui.error(str(exc))
        if trace is not None:
            trace.status = "selection_failed"
            trace.selection_notes.append(str(exc))
        return sp500_bench_return, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), survivors_df, []
    with spinner("Analyzing Dominant Sectors per Factor..."):
        metrics_df, loadings_df, factor_headers = engine.label_factors(metrics_df, loadings_df)
    with spinner("Executing Phase 5: Selection & Portfolio Assembly..."):
        result = engine.assemble_portfolio(
            sp500_bench_return, metrics_df, loadings_df, survivors_df,
            factor_headers, shadow_list, target_portfolio_size)
        if trace is not None:
            trace.finish(result[3])
        return result


def _label(value):
    if value is None:
        return None
    return value.date().isoformat() if isinstance(value, pd.Timestamp) else str(value)
