import pandera.pandas as pa
import pandas as pd
import numpy as np

def validate_returns_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validates that the returns matrix contains valid numerical data,
    no infinities, no NaNs, and values fall within expected daily return bounds.
    """
    # Dynamically apply checks to all columns (tickers) using regex '.*'
    schema = pa.DataFrameSchema(
        columns={
            ".*": pa.Column(
                float,
                checks=[
                    # Ensure no infinite values
                    pa.Check(lambda s: ~s.isin([np.inf, -np.inf]).any(), error="Contains infinite values"),
                    # Ensure daily returns are within reasonable theoretical limits (-99% to +1000%)
                    pa.Check.in_range(-0.99, 10.0, error="Returns out of expected daily bounds (-0.99 to 10.0)")
                ],
                nullable=False, # We already dropped NaNs in app.py, this enforces it
                regex=True
            )
        },
        coerce=True # Attempts to coerce data types to float if needed
    )
    return schema.validate(df)
