"""Pure portfolio-selection transformations over already-fetched inputs."""

import logging

import numpy as np
import pandas as pd
from factor_analyzer import FactorAnalyzer

from data_errors import BenchmarkError, MarketCapError, positive_market_cap, summarize_tickers
from metrics import calculate_downside_beta
from symbol_utils import normalize_symbol
from validation import validate_returns_matrix


class SelectionError(Exception):
    """A handled selection failure whose message is displayed by the adapter."""


DOWNSIDE_BETA_WEEKS = 104
PRICE_HISTORY_DAYS = (DOWNSIDE_BETA_WEEKS + 2) * 7
# Weekly closes in the trailing-12-month window (52 weekly returns); used for stock
# returns and, in default gate mode, the SPY benchmark threshold.
T12M_WEEKLY_CLOSES = 53
NON_SHADOW_MIN_LOADING = 0.59
logger = logging.getLogger(__name__)


def prepare_universe(sp500_df, banned_tickers):
    screened_df = sp500_df[sp500_df["GICS Sector"] != "Energy"].copy()
    ticker_to_sector = {
        normalize_symbol(t): s
        for t, s in zip(screened_df["Symbol"], screened_df["GICS Sector"])
    }
    candidates = [t for t in ticker_to_sector.keys() if t not in banned_tickers]
    return candidates, ticker_to_sector


def validate_market_caps(candidates, mcaps_dict):
    """Normalize unknown caps to zero, warn, and preserve candidate order for ties."""
    if not candidates:
        raise MarketCapError("No screened S&P 500 candidates were available to rank by market cap.")
    caps, unknown = {}, []
    for ticker in candidates:
        cap = positive_market_cap(mcaps_dict.get(ticker))
        if cap is None:
            unknown.append(ticker)
        caps[ticker] = cap if cap is not None else 0.0
    if unknown:
        logger.warning(market_cap_warning(unknown))
    return caps


def market_cap_warning(tickers):
    return (
        f"Market caps unavailable for {len(tickers)} tickers; assigned zero and ranked last: "
        f"{summarize_tickers(tickers)}. Market-cap ranking may be incomplete; "
        "if all caps are unavailable, constituent order determines the universe."
    )


def select_universe(candidates, mcaps_dict, universe_size):
    caps = validate_market_caps(candidates, mcaps_dict)
    market_cap_sorted_tickers = sorted(candidates, key=lambda t: caps[t], reverse=True)
    return market_cap_sorted_tickers[:universe_size] + ["SPY"]


def trailing_window_bounds(data):
    """First and last index labels of the trailing-12-month price window."""
    index = data.index[-T12M_WEEKLY_CLOSES:]
    return (index[0], index[-1]) if len(index) else (None, None)


def prepare_returns(data):
    # Fill before slicing: prices preceding the trailing window can fill its first row.
    data = data.ffill()
    t12m_prices = data.iloc[-T12M_WEEKLY_CLOSES:]
    t12m_prices = t12m_prices.dropna(axis=1)
    if "SPY" not in t12m_prices.columns or len(t12m_prices) < 2:
        raise BenchmarkError(
            "SPY weekly closes are missing from the trailing-12-month price window; "
            "the benchmark return cannot be calculated.")
    t12m_returns = (t12m_prices.iloc[-1] / t12m_prices.iloc[0]) - 1.0
    sp500_bench_return = t12m_returns["SPY"]
    if not np.isfinite(sp500_bench_return):
        raise BenchmarkError("SPY trailing-12-month return is not finite.")
    returns_df = t12m_prices.pct_change().dropna(how='all')
    bench_ret = returns_df["SPY"]
    return sp500_bench_return, t12m_returns, returns_df, bench_ret


def prepare_downside_returns(data):
    # Fill before slicing, retaining partial histories for pairwise NaN handling.
    prices = data.ffill().iloc[-(DOWNSIDE_BETA_WEEKS + 1):]
    returns_df = prices.pct_change(fill_method=None).dropna(how="all")
    return returns_df, returns_df["SPY"]


def screen_survivors(t12m_returns, returns_df, bench_ret, mcaps_dict, gate_threshold):
    if gate_threshold is not None:
        beat_bench = t12m_returns[t12m_returns >= gate_threshold].drop(labels=["SPY"], errors="ignore")
    else:
        beat_bench = t12m_returns.drop(labels=["SPY"], errors="ignore")

    survivor_tickers = beat_bench.index.tolist()
    downside_betas = [
        calculate_downside_beta(returns_df[t], bench_ret) if t in returns_df.columns else np.nan
        for t in survivor_tickers
    ]
    survivors_df = pd.DataFrame({
        "Ticker": survivor_tickers,
        "T12M_Return": [beat_bench.get(t, np.nan) for t in survivor_tickers],
        "Market_Cap": [mcaps_dict.get(t, np.nan) for t in survivor_tickers],
        "Downside_Beta": downside_betas
    }).sort_values(by="Market_Cap", ascending=False).set_index("Ticker")
    return survivor_tickers, survivors_df


def fit_factors(returns_df, survivor_tickers, n_factors):
    valid_for_fa_tickers_raw = [t for t in survivor_tickers if t in returns_df.columns]
    if not valid_for_fa_tickers_raw:
        raise SelectionError("No survivors passed the performance gate and have sufficient data for factor analysis!")

    returns_matrix_fa_full = returns_df[valid_for_fa_tickers_raw]
    returns_matrix_fa = returns_matrix_fa_full.dropna()
    valid_for_fa_tickers = returns_matrix_fa.columns.tolist()
    if returns_matrix_fa.empty or len(valid_for_fa_tickers) < 1:
        raise SelectionError("Returns matrix for factor analysis is empty or has too few valid tickers after handling NaNs!")

    n_factors_actual = min(n_factors, len(valid_for_fa_tickers))
    if n_factors_actual < 1:
        raise SelectionError("Not enough tickers for factor analysis to extract any factors.")

    # Retain both validation calls and their order from the original pipelines.
    try:
        returns_matrix_fa = validate_returns_matrix(returns_matrix_fa)
    except Exception as e:
        raise SelectionError(f"Data validation failed prior to factor analysis: {e}") from e
    try:
        returns_matrix_fa = validate_returns_matrix(returns_matrix_fa)
    except Exception as e:
        raise SelectionError(f"Data validation failed prior to factor analysis: {e}") from e

    fa = FactorAnalyzer(n_factors=n_factors_actual, rotation="varimax", method="minres")
    try:
        fa.fit(returns_matrix_fa)
    except ValueError as e:
        raise SelectionError(
            f"Factor analysis failed: {e}. This might be due to insufficient data or correlation issues."
        ) from e

    loadings_df = pd.DataFrame(
        fa.loadings_,
        index=valid_for_fa_tickers,
        columns=[f"Factor_{i+1}" for i in range(n_factors_actual)]
    )
    max_loadings = loadings_df.abs().max(axis=1)
    dominant_factors = loadings_df.abs().idxmax(axis=1)
    return valid_for_fa_tickers, loadings_df, max_loadings, dominant_factors


def score_metrics(valid_for_fa_tickers, returns_df, bench_ret, t12m_returns,
                  max_loadings, dominant_factors, ticker_to_sector, shadow_cutoff):
    metrics = []
    for ticker in valid_for_fa_tickers:
        d_beta = calculate_downside_beta(returns_df[ticker], bench_ret)
        if not np.isnan(d_beta):
            metrics.append({
                "Ticker": ticker,
                "T12M_Return": t12m_returns.get(ticker, np.nan),
                "Downside_Beta": d_beta,
                "Max_Factor_Loading": max_loadings.get(ticker, np.nan),
                "Dominant_Factor": dominant_factors.get(ticker, "N/A"),
                "Sector": ticker_to_sector.get(ticker, "Unknown")
            })

    if not metrics:
        raise SelectionError("No valid stocks after Downside Beta calculation!")
    metrics_df = pd.DataFrame(metrics).set_index("Ticker")

    metrics_df["Max_Factor_Loading"] = metrics_df.index.map(max_loadings)
    metrics_df["Dominant_Factor"] = metrics_df.index.map(dominant_factors)
    shadow_mask = metrics_df["Max_Factor_Loading"] < shadow_cutoff
    shadow_list = metrics_df[shadow_mask].sort_values(by="Downside_Beta", ascending=True).index.tolist()
    return metrics_df, shadow_list


def label_factors(metrics_df, loadings_df):
    metrics_df = metrics_df.copy()
    loadings_df = loadings_df.copy()
    factor_headers = []
    for col in loadings_df.columns:
        dom_tickers = metrics_df[metrics_df["Dominant_Factor"] == col].index
        if not dom_tickers.empty:
            sectors = metrics_df.loc[dom_tickers, "Sector"].value_counts()
            top_sector = sectors.index[0] if not sectors.empty else "Unknown"
            factor_headers.append(f"{col}\n({top_sector})")
        else:
            factor_headers.append(f"{col}\n(Mixed)")

    loadings_df.columns = factor_headers
    metrics_df["Dominant_Factor"] = metrics_df["Dominant_Factor"].map(
        lambda x: factor_headers[int(x.split('_')[1])-1] if isinstance(x, str) and x.startswith('Factor_') else x
    )
    return metrics_df, loadings_df, factor_headers


def assemble_portfolio(sp500_bench_return, metrics_df, loadings_df, survivors_df,
                       factor_headers, shadow_list, target_portfolio_size, *,
                       shadow_fraction_limit=None, shadow_priority="downside_beta"):
    if shadow_priority not in {"downside_beta", "trailing_return"}:
        raise ValueError("Unsupported shadow priority.")
    if shadow_priority == "trailing_return":
        priority = metrics_df.loc[list(dict.fromkeys(shadow_list))].copy()
        priority["_return"] = pd.to_numeric(priority["T12M_Return"], errors="coerce")
        priority["_ticker"] = priority.index
        shadow_list = priority.sort_values(
            ["_return", "Downside_Beta", "_ticker"],
            ascending=[False, True, True], na_position="last",
        ).index.tolist()
    if shadow_fraction_limit is not None:
        if shadow_fraction_limit != 0.25:
            raise ValueError("Only a 25% shadow limit is supported.")
        # Try the largest feasible priority allocation against actual portfolio size.
        shadow_limit = target_portfolio_size // 4
        shadow_set = set(shadow_list)
        if (loadings_df.loc[loadings_df.index.isin(shadow_set), factor_headers]
                .abs().ge(NON_SHADOW_MIN_LOADING).any().any()):
            raise SelectionError("Shadow candidates unexpectedly qualify as non-shadow candidates.")
        while True:
            eligible_shadow = []
            counts = {}
            for ticker in shadow_list:
                dominant = metrics_df.loc[ticker, "Dominant_Factor"]
                if (ticker not in eligible_shadow and counts.get(dominant, 0) < 3
                        and len(eligible_shadow) < shadow_limit):
                    eligible_shadow.append(ticker)
                    counts[dominant] = counts.get(dominant, 0) + 1
            result = assemble_portfolio(
                sp500_bench_return, metrics_df, loadings_df, survivors_df,
                factor_headers, eligible_shadow, target_portfolio_size)
            portfolio = result[3]
            allowed = len(portfolio) // 4
            if len(eligible_shadow) <= allowed:
                return result
            shadow_limit -= 1
    factor_counts = {f: 0 for f in factor_headers}
    selected = []

    # 1. Add from shadow list, enforcing max 3 per dominant factor
    for ticker in shadow_list:
        if len(selected) >= target_portfolio_size:
            break
        dom_f = metrics_df.loc[ticker, "Dominant_Factor"]
        if ticker not in selected and factor_counts.get(dom_f, 0) < 3:
            selected.append(ticker)
            factor_counts[dom_f] = factor_counts.get(dom_f, 0) + 1

    loadings_abs = loadings_df.abs()
    non_shadow = metrics_df.loc[~metrics_df.index.isin(shadow_list)]
    returns = pd.to_numeric(non_shadow["T12M_Return"], errors="coerce")
    ranked_non_shadow = non_shadow.assign(
        _selection_return=returns.where(np.isfinite(returns)),
        _selection_ticker=non_shadow.index,
    ).sort_values(
        by=["_selection_return", "Max_Factor_Loading", "Downside_Beta", "_selection_ticker"],
        ascending=[False, False, True, True], na_position="last",
    )

    # 2. Select the highest-return eligible non-shadow candidate per factor
    for col in factor_headers:
        if len(selected) >= target_portfolio_size:
            break
        filtered_candidates = [
            c for c in ranked_non_shadow.index
            if c not in selected and c in loadings_abs.index
            and loadings_abs.loc[c, col] >= NON_SHADOW_MIN_LOADING
        ]
        if filtered_candidates:
            for candidate in filtered_candidates:
                dom_f = metrics_df.loc[candidate, "Dominant_Factor"]
                if factor_counts.get(dom_f, 0) < 3:
                    selected.append(candidate)
                    factor_counts[dom_f] = factor_counts.get(dom_f, 0) + 1
                    break

    # 3. Fill remaining slots if needed (safety net) to hit target size
    if len(selected) < target_portfolio_size:
        remaining_shadow = metrics_df.loc[
            metrics_df.index.isin(shadow_list) & ~metrics_df.index.isin(selected)
        ]
        if shadow_priority == "trailing_return":
            remaining_shadow = remaining_shadow.assign(
                _return=pd.to_numeric(remaining_shadow["T12M_Return"], errors="coerce"),
                _ticker=remaining_shadow.index,
            ).sort_values(
                ["_return", "Downside_Beta", "_ticker"],
                ascending=[False, True, True], na_position="last",
            ).index.tolist()
        else:
            remaining_shadow = remaining_shadow.sort_values(
                by="Downside_Beta", ascending=True,
            ).index.tolist()
        qualifying = loadings_abs[factor_headers].ge(NON_SHADOW_MIN_LOADING).any(axis=1)
        remaining_non_shadow = [
            c for c in ranked_non_shadow.index
            if c not in selected and qualifying.get(c, False)
        ]
        for candidate in remaining_shadow + remaining_non_shadow:
            if len(selected) >= target_portfolio_size:
                break
            dom_f = metrics_df.loc[candidate, "Dominant_Factor"]
            if factor_counts.get(dom_f, 0) < 3:
                selected.append(candidate)
                factor_counts[dom_f] = factor_counts.get(dom_f, 0) + 1

    final_tickers = selected[:target_portfolio_size]
    if len(final_tickers) == 0:
        return sp500_bench_return, metrics_df, loadings_df, pd.DataFrame(), survivors_df, factor_headers

    weight = 100.0 / len(final_tickers)
    portfolio_df = metrics_df.loc[final_tickers].copy()
    portfolio_df["Weight (%)"] = round(weight, 2)
    portfolio_df["In_Shadow_List"] = portfolio_df.index.isin(shadow_list)
    return sp500_bench_return, metrics_df, loadings_df, portfolio_df, survivors_df, factor_headers
