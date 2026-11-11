import streamlit as st
from ui_style import apply_nav_style
from access_gate import legal_footer, require_access

st.set_page_config(page_title="How the CB Tax-Exempt Protocol Works", layout="wide")
apply_nav_style()
require_access()
st.title("How the CB Tax-Exempt Protocol Works")

st.markdown(
    """
### In Memory and In Honor of Michael H. Bullington

Michael H. Bullington was co-creator of the CB Portfolio Protocol and responsible for the periodic calculations derived from the CB Protocol logic, contributing innumerable insights over its 21-year history. He and William R. Camp were close personal friends for 40 years, until Mike's passing from complications of glioblastoma on October 14, 2026. He was a brilliant, witty, graceful and humble collaborator. He was a true and loyal friend, the most prized blessing anyone can have in life. William R. Camp developed this app in his honor. Rest in peace, my prized friend.
"""
)

st.divider()

st.markdown(
    """
### How the Screening Works

The protocol screens S&P 500 companies and builds an equally weighted sample portfolio. It is a research tool, not a promise of investment returns.

#### 1. Set the date and starting universe

The app uses the current S&P 500 constituent list and current market caps, even when you select an earlier date. It ranks eligible companies by current market cap and considers up to the selected universe size, which defaults to 150. The date you choose controls the historical price data; it does not select the S&P 500 members or market caps that existed on that date.

#### 2. Apply exclusions and the return screen

The app excludes Energy-sector companies and specified oil, gold, cryptocurrency, and duplicate-share tickers. The minimum-return gate is on by default: a stock must meet or exceed SPY's trailing return over the same 52 weekly returns (53 weekly closes). You can instead set a custom minimum or turn the gate off. Missing or invalid market caps are assigned zero and ranked last, with a warning; if all caps are unavailable, constituent order determines the universe. The app stops if required SPY data is unavailable. Stocks without the price history needed for the screen are not considered.

SPY, a fund that follows the S&P 500, is also used as the comparison benchmark.

#### 3. Compare market patterns and downside behavior

For eligible stocks, the app analyzes about one year of weekly returns using factor analysis, then estimates downside beta relative to SPY using up to about two years of weekly returns. Downside beta uses weeks when SPY fell and requires at least 12 such weeks with usable data. These are historical measures, not predictions.

#### 4. Select and weight the portfolio

Stocks whose maximum absolute factor loading is below the configured shadow-list cutoff are considered first, ordered by downside beta. The app then considers other stocks with an absolute factor loading of at least 0.59, favoring higher trailing returns and spreading selections across dominant factors. It selects no more than three stocks per dominant factor. The portfolio may contain fewer stocks than the requested target if there are not enough eligible candidates. Holdings actually selected receive equal weights.

#### 5. Historical portfolio updates

Historically, actual portfolio holdings have been updated once a year in early January to account for shifts in market sentiment and business attractiveness, while minimizing capital gains taxes in taxable portfolios.
"""
)

st.divider()

st.markdown(
    """
### Testing Earlier Years

A backtest asks what might have happened if the app had selected stocks at the end of each year and held them through the next.

**Important limitation:** The current backtest uses today's S&P 500 constituents and market caps for historical rebalance dates, then checks historical prices for those companies. It does not reconstruct historical index membership or include former constituents that are no longer in today's index. As a result, the saved backtest is subject to survivorship bias and is not a point-in-time simulation of the S&P 500 universe as it existed in each year. The App and Backtest will be updated with commercial data sources to mitigate this limitation in the near future.

The saved 2016–2025 backtest beat the S&P 500 in 9 of 10 years, but lagged in 2023. SPIVA reported that 84.3% of U.S. large-company funds run by professional managers underperformed the S&P 500 over the decade ending in 2024. The periods, methods, and survivorship limitations differ, so this is not a direct comparison or a prediction of future results.

S&P 500 membership changes, and companies may merge, split, change tickers, or disappear. Historical prices may also be missing for some current constituents. Past results do not guarantee future results.
"""
)

legal_footer()
