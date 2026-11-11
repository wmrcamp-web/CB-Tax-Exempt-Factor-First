import pytest
import pandas as pd
import numpy as np
from metrics import calculate_downside_beta

def test_calculate_downside_beta_normal():
    # Create synthetic data where asset perfectly tracks downside with a multiplier
    bench = pd.Series([-0.02, -0.01, -0.03, 0.01, 0.02] * 10) # 50 points, 30 downside
    asset = bench * 1.5
    
    beta = calculate_downside_beta(asset, bench)
    assert not np.isnan(beta)
    assert pytest.approx(beta, 0.01) == 1.5

def test_calculate_downside_beta_insufficient_data():
    # Only 6 downside points, function requires 12
    bench = pd.Series([-0.02, -0.01, -0.03, 0.01, 0.02] * 2)
    asset = bench * 1.5
    
    beta = calculate_downside_beta(asset, bench)
    assert np.isnan(beta)


@pytest.mark.parametrize("down_weeks", [11, 12, 19, 20])
def test_calculate_downside_beta_weekly_sample_threshold(down_weeks):
    bench = pd.Series(
        np.r_[np.linspace(-0.03, -0.01, down_weeks), np.full(52 - down_weeks, 0.02)],
        index=pd.date_range("2024-01-05", periods=52, freq="W-FRI"),
    )
    asset = bench * 1.5
    beta = calculate_downside_beta(asset, bench)

    if down_weeks < 12:
        assert np.isnan(beta)
    else:
        assert beta == pytest.approx(1.5)


def test_calculate_downside_beta_counts_only_complete_downside_pairs():
    bench = pd.Series(np.linspace(-0.03, -0.01, 12))
    asset = bench * 1.5
    asset.iloc[0] = np.nan

    assert np.isnan(calculate_downside_beta(asset, bench))


@pytest.mark.parametrize("values", [[], [0.02] * 52, [-0.125] * 12])
def test_calculate_downside_beta_invalid_samples(values):
    bench = pd.Series(values, dtype=float)

    assert np.isnan(calculate_downside_beta(bench * 1.5, bench))
