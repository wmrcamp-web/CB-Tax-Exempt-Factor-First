import pandas as pd
import numpy as np

def calculate_downside_beta(asset_returns, benchmark_returns):
    """Calculates the downside beta of an asset relative to a benchmark."""
    combined = pd.DataFrame({"Asset": asset_returns, "Benchmark": benchmark_returns}).dropna()
    downside = combined[combined["Benchmark"] < 0]

    if len(downside) < 12:
        return np.nan

    cov = np.cov(downside["Asset"], downside["Benchmark"])[0, 1]
    var_bench = np.var(downside["Benchmark"], ddof=1) # Fixed: match ddof=1 used by np.cov

    if var_bench == 0:
        return np.nan
    return cov / var_bench
