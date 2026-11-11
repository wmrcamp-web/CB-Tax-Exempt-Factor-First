# Backtest Survivorship-Bias Remediation Plan

Status: **audit/trace foundation implemented** — historical source adapters and a
point-in-time provider remain future work. Live defaults and calculations are unchanged.

## Current state (what's live on `main`)

- Weekly price sampling everywhere: `interval="1wk"` downloads in `app.py`,
  `cb_taxable_portfolio_app.py`, `data_fetcher.py` (SPY benchmark), and `backtest_engine.py`
  (forward returns — documented decision).
- Trailing-12-month gate and factor analysis = last 53 weekly closes (~52 weekly returns).
  Downside beta uses the last 105 weekly closes (104 weekly returns), with at least 12
  aligned benchmark down weeks and nonzero benchmark variance. Selection fetches 742 days
  of weekly prices to include a week-boundary buffer; shorter histories use available pairs.
- Mocked test suite `test_weekly_sampling.py` (8 tests) + `test_metrics.py` (2 tests): 10/10 passing.
  Run: `DISABLE_PANDERA_IMPORT_WARNING=True python -m pytest`
- UI: How It Works page (`pages/2_How_It_Works.py`) with all-caps disclaimer, red disclaimer
  banner on both apps' main pages, large bold uppercase sidebar nav via `ui_style.py`.
- Deployment: Streamlit Community Cloud auto-deploys from GitHub `main`. All dev work for
  the items below should happen on a **feature branch**, merged only when validated.

## Problem: survivorship bias in the backtest

`backtest_engine.py` calls `run_pipeline` for each historical year, but the universe and
rankings come from *today's* data:

- `fetch_sp500_tickers()` scrapes the current Wikipedia S&P 500 list (only survivors).
- `get_sorted_by_market_cap()` uses current `marketCap` via yfinance, not caps as of the
  rebalance date.

Also noted: `Holdings_Returns` column in `backtest_results.csv` is defined but never populated.

## Data needed for a point-in-time backtest

| Dataset | Free options | Paid/robust |
|---|---|---|
| Constituents per Dec 31 | Wikipedia revision history (MediaWiki API) or GitHub snapshot repos | CRSP, Compustat, Norgate, Sharadar |
| Historical market caps | Approximate: current shares outstanding x price at rebalance date (documented approximation) | Compustat CSHO, Norgate |
| Delisted ticker prices | Hard for free — yfinance lacks most acquired/delisted names | Norgate/Sharadar delisted DBs |

Caveats: pipeline silently drops NaN tickers (`dropna(axis=1)`) — log coverage per year
("2015 universe: 487/500 found") so residual bias is measured, not hidden. Maintain a
`symbol_map.csv` for ticker renames (FB->META). Standardize on adjusted closes (dividends).

## Cache design (immutable history = permanent cache)

The Parquet layout below is the original design. The local audit foundation uses
the CSV contract described at the end of this document; it does not implement
Parquet loaders or a snapshot-consuming provider.

Historical data never changes -> cache forever; only the current partial year is mutable.

```
data_cache/
  constituents/sp500_YYYY-12-31.parquet   # one immutable snapshot per rebalance date
  market_caps/mcaps_YYYY-12-31.parquet    # ticker -> approx cap at that date
  prices/<TICKER>.parquet                 # per-ticker append-only weekly closes
  _meta.json                              # schema version, source, fetched_at
```

- Parquet, fetch-on-miss loaders, disk cache as source of truth + `st.cache_data` as session layer.
- Invalidation by date, not time; version the schema; audit fields in every file.

## Architecture decision (agreed): shared core, swappable data providers

Do NOT duplicate the pipeline into a standalone backtest (drift risk). Extract once:

- `protocol_engine.py` (to be created): pure functions — `select_portfolio(prices, universe, params)`,
  no streamlit/network imports. `app.py` / `cb_taxable_portfolio_app.py` become thin UI shells.
- Provider interface: `LiveProvider` (Wikipedia + yfinance — stays the app default; the live app
  SHOULD use current constituents) and `PointInTimeProvider` (parquet snapshots — injected by the backtest).
- Feature flag `CB_DATA_PROVIDER=pit` (default `live`) so merging is non-disruptive.

## Factor-analysis caveat (weekly sampling)

FA now fits on ~52 weekly returns per stock — thin for 50-100 survivors (minres runs fine,
but loadings are noisier). Damage is bounded because final weights are equal (1/N); loadings
only drive grouping/cutoffs. Recommended when resuming: decouple windows — keep 53-week window
for the T12M gate, feed FA 104-156 weekly closes (2-3 yrs; increase fetch history if needed).
Optionally add `calculate_kmo()` / `calculate_bartlett_sphericity()` diagnostics (KMO > 0.6 floor).

## Stability-first rollout (each step a separate green merge)

1. Behavior-preserving refactor: extract core engine; app output byte-identical (pytest + Streamlit AppTest).
2. Provider interface with `LiveProvider` default — no behavior change.
3. Offline snapshot builder script (standalone, never imported by apps) -> writes `data_cache/`.
4. Backtest switches to `PointInTimeProvider`; writes `backtest_results_pit.csv` (never overwrites
   the production CSV); diff per year vs `backtest_results.csv` to quantify the bias effect.
5. Reproducibility test: two backtest runs from cache -> identical outputs.
6. Optional CI: GitHub Actions running pytest (+ AppTest smoke) on PRs; tag a release before step 1.

## Testing without disrupting the live app

- Feature branch (+ optional second Streamlit Cloud staging app pointed at the branch).
- Pure core = plain pytest, no import-time Streamlit mocking needed.
- Parallel CSV comparison; reproducibility anchor test; AppTest headless UI regression checks.

## Local snapshot audit and builder scaffold

`snapshot_audit.py` reads local files only and never imports apps, calls providers,
downloads data, or executes fallback. Default return years 2015–2026 imply
rebalance dates **2014-12-31 through 2025-12-31**. Override the return-year range
for later backtests.

CSV input contract:

```text
data_cache/constituents/sp500_YYYY-MM-DD.csv
  Symbol,Security,GICS Sector,GICS Sub-Industry
data_cache/market_caps/mcaps_YYYY-MM-DD.csv
  Symbol,MarketCap
data_cache/_meta.json                         # optional provenance manifest
```

Symbols use the engine's `.` → `-` normalization (no inferred renames or automatic
uppercasing). Symbols must be nonempty and contain no whitespace. Caps must be
numeric, finite, and nonnegative. Both files must be readable, nonempty, and have
the required columns; duplicates after normalization invalidate the pair.
Every constituent, including names later excluded by sector screening, must have
a valid cap. Extra cap symbols are reported as mismatches but do not invalidate
an otherwise complete pair.

The optional manifest is keyed by rebalance date, for example:

```json
{
  "2025-12-31": {
    "schema_version": 1,
    "constituents": {
      "as_of_date": "2025-12-31",
      "source": "historical source export",
      "retrieved_at": "2026-10-13T00:00:00Z",
      "methodology": "historical_observation",
      "sha256": "checksum of the CSV",
      "symbol_mapping": {}
    },
    "market_caps": {
      "as_of_date": "2025-12-31",
      "source": "historical source export",
      "methodology": "approximation"
    }
  }
}
```

The audit preserves provenance and rejects conflicting represented dates or
malformed metadata. Missing provenance is `unverified`; `provided` means metadata
was supplied, **not** that its accuracy, checksums, or historical methodology were
verified. `snapshot_complete` means only local structural completeness of the file
pair; it does not verify historical source data. File completeness alone is not
evidence that survivorship bias is fixed.

Run from the repository root (substitute absolute local input/output paths):

```bash
python snapshot_audit.py --cache-dir /path/to/data_cache --output /tmp/snapshot_coverage.json
python snapshot_audit.py --cache-dir /path/to/data_cache --start-year 2015 --end-year 2026 --output /tmp/snapshot_coverage.csv
```

Reports contain one row per date, pair status, row/unique-symbol counts, valid cap
coverage, missing/invalid tickers, invalid/duplicate symbol counts, symbol
mismatches, provenance, and failure reasons. `would_require_live_fallback` predicts
a **proposed strict whole-date policy**, not actual provider behavior.
`current_provider_source` is always `live_current`: this checkout has only
`LiveProvider`, which does not look up snapshots even when the pair is complete.
There is no `provider_accepts_pair` claim until a PIT provider exists.

### Historical builder/source-adapter specification (not implemented)

- Import genuine dated source exports into the CSV contract above; preserve
  original symbols and record any explicit mapping in the manifest.
- Record represented date, source, retrieval time, schema version, methodology,
  and artifact checksums. Reconcile both inputs to the same rebalance date.
- Never fill historical gaps with current constituents or market caps.
  Preserve missing/invalid records so the audit can expose coverage gaps.
- Do not overwrite historical artifacts by default; require explicit approval.
- Historical market-cap sourcing remains unresolved. Do not silently use current
  shares × historical price. Label experimental approximations `approximation`,
  separately from `historical_observation` and missing values.
- Price/delisting coverage and trusted provenance verification remain separate
  future requirements; a complete constituent/cap pair is only the foundation.

## Optional per-period selection trace

Both apps still return the same six items. An optional `SelectionTrace` captures
the rebalance date, gate, provider class, normalized constituent universe,
sector/banned-name screened universe, cap-ranked universe, price-eligible names,
survivors, successfully factor-fitted names, final tickers, and trailing benchmark
return. Handled selection failures preserve available stages and an error note.
This trace is descriptive only; it does not change selection.

```python
from selection_trace import SelectionTrace, write_selection_traces
from app import run_pipeline

trace = SelectionTrace()
results = run_pipeline(rebalance_date, gate_threshold, trace=trace)
write_selection_traces([trace], "/tmp/selection_trace.json")
```

`weights_percent` preserves the displayed rounded `Weight (%)` values;
`weights_fraction` records the exact equal weights used by the backtest.
The yearly backtest accepts a collector without changing its return dictionary:

```python
from backtest_engine import run_backtest_for_year, update_backtest_history
from selection_trace import write_selection_traces

traces = []
result = run_backtest_for_year(2026, traces=traces)
write_selection_traces(traces, "/tmp/backtest_selection_trace.json")

update_backtest_history(
    csv_path="/tmp/experimental_backtest.csv",
    start_year=2015,
    trace_path="/tmp/backtest_selection_trace.json",
)
```

Backtest traces also record the return year, forward benchmark return, and
backtest outcome (including failed benchmark fetches and forward downloads).
When history tracing is requested, each attempted period is persisted before
summary CSV years are marked computed; benchmark errors still propagate as before.
JSON records merge by
rebalance date/year, retaining earlier periods. Nonfinite numbers serialize as
JSON `null`. No trace file is written unless explicitly requested.
Already-computed CSV years are still skipped: tracing does not reconstruct
historical portfolios from summary returns. Re-run a year explicitly or use a
separate experimental CSV to capture its holdings. The provider class identifies
the run's provider but does not certify historical input provenance.
