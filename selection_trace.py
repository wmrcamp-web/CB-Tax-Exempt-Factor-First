"""Optional selection diagnostics, independent of portfolio return calculations."""

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path


@dataclass
class SelectionTrace:
    schema_version: int = 2
    rebalance_date: str = ""
    portfolio_year: int | None = None
    gate_threshold: float | None = None
    # "benchmark": SPY T12M from the run's own price window; "fixed": caller-supplied
    # threshold; "off": no performance gate.
    gate_mode: str | None = None
    benchmark_window_start: str | None = None
    benchmark_window_end: str | None = None
    provider_type: str = ""
    constituent_universe: list[str] = field(default_factory=list)
    screened_universe: list[str] = field(default_factory=list)
    market_cap_universe: list[str] = field(default_factory=list)
    price_eligible_tickers: list[str] = field(default_factory=list)
    survivor_tickers: list[str] = field(default_factory=list)
    factor_eligible_tickers: list[str] = field(default_factory=list)
    final_tickers: list[str] = field(default_factory=list)
    weights_percent: dict[str, float] = field(default_factory=dict)
    weights_fraction: dict[str, float] = field(default_factory=dict)
    benchmark_return: float | None = None
    forward_benchmark_return: float | None = None
    status: str = "started"
    backtest_status: str | None = None
    selection_notes: list[str] = field(default_factory=list)

    def begin(self, date, gate_threshold, provider, gate_mode=None):
        self.__dict__.update(asdict(SelectionTrace(
            rebalance_date=date.isoformat(),
            portfolio_year=date.year + 1,
            gate_threshold=gate_threshold,
            gate_mode=gate_mode or ("off" if gate_threshold is None else "fixed"),
            provider_type=type(provider).__name__,
        )))

    def fail(self, exc):
        self.status = getattr(exc, "trace_status", "selection_failed")
        self.selection_notes.append(str(exc))

    def finish(self, portfolio):
        self.final_tickers = portfolio.index.tolist()
        self.weights_percent = (
            {ticker: float(weight) for ticker, weight in portfolio["Weight (%)"].items()}
            if not portfolio.empty else {}
        )
        self.weights_fraction = (
            dict.fromkeys(self.final_tickers, 1.0 / len(self.final_tickers))
            if self.final_tickers else {}
        )
        self.status = "selected" if self.final_tickers else "empty_portfolio"


def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def write_selection_traces(traces, output_path):
    """Merge by rebalance date/year; do not erase records from earlier runs."""
    path = Path(output_path)
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    by_period = {(row["rebalance_date"], row["portfolio_year"]): row for row in records}
    for trace in traces:
        by_period[(trace.rebalance_date, trace.portfolio_year)] = _json_safe(asdict(trace))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(list(by_period.values()), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
