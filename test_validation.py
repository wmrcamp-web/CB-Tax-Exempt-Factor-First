"""Regression tests for warning-free pandas returns validation."""

import warnings

import numpy as np
import pandas as pd
import pytest
from pandera.errors import SchemaError

from validation import validate_returns_matrix


def test_valid_returns_validate_without_future_warnings():
    returns = pd.DataFrame({
        "AAA": [0.01, -0.02, 0.0],
        "BBB": [-0.99, 10.0, 0.03],
    }, index=pd.date_range("2024-01-05", periods=3, freq="W-FRI"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        validated = validate_returns_matrix(returns)
    pd.testing.assert_frame_equal(validated, returns)


def test_numeric_returns_are_still_coerced_to_float():
    returns = pd.DataFrame({"AAA": ["0.01", "-0.02"], "BBB": [0, 1]})
    validated = validate_returns_matrix(returns)
    pd.testing.assert_frame_equal(validated, returns.astype(float))


@pytest.mark.parametrize("invalid_return", [np.nan, np.inf, -np.inf, -1.0, 10.01])
def test_invalid_returns_are_still_rejected(invalid_return):
    returns = pd.DataFrame({"AAA": [0.01, invalid_return]})
    with pytest.raises(SchemaError):
        validate_returns_matrix(returns)
