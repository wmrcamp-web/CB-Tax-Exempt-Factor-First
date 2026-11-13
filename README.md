# Tax Exempt Factor First

Variant of CB Tax Exempt Protocol: factor analysis runs on the full universe first, then the T12M benchmark gate is applied. Backtest history (`backtest_results.csv`) is inherited from the original and not regenerated.


## Legal cover sheet (no sign-in or database setup required)

This change is code and documentation only: **it does not deploy the app or change
deployment settings**. Both dashboards and every directly navigable page show a
cover sheet before app controls, results, or downloads. It displays investment-risk
and AI-content warnings, the Terms of Service / Terms of Use, Privacy Notice, and
Legal Notice, with personal ownership:
**© 2026 William R. Camp. All rights reserved.** Contact: **wmrcamp@gmail.com**.

The cover sheet states:

> BY OPENING THE APP, YOU HAVE READ ALL THE USE WARNINGS AND ARE AGREEING TO THE TERMS OF SERVICE.

Click **Open App** to acknowledge the notices and enter. There are no separate
checkboxes. Visitors who disagree should close the page. This is not a verified
identity system or a guarantee of legal enforceability; seek legal advice as needed.

### Run locally

From the repository root:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The alternate dashboard is `python -m streamlit run cb_taxable_portfolio_app.py`.
**No Google account, OAuth credentials, Streamlit secrets, Neon database, or
PostgreSQL installation is needed.** Existing identity configuration is not read
by the cover sheet.

### Opt-in Sharadar historical backtests

The existing dashboards and baseline backtests still use the live provider.
`sharadar_provider.py` supports **Sharadar's direct API**, not Nasdaq Data Link.
It requests historical S&P 500 snapshots and effective membership changes,
dated `daily.marketcap` (converted from USD millions to USD), adjusted stock
prices, adjusted SPY fund prices, and security metadata. Required subscriptions
cover the `sp500`, `daily`, `stocks`, `funds`, and `tickers` tables. See the vendor's
[direct API documentation](https://sharadar.com/docs/intro).

**Do not paste API keys into code, chat, command arguments, or committed files.**
Revoke any exposed key. Configure its replacement as `SHARADAR_API_KEY` through
your local environment, or through private `.streamlit/secrets.toml` for viewing
and rebuilding results in Streamlit:

```toml
SHARADAR_API_KEY = "REPLACE_WITH_PRIVATE_KEY"
# Optional; default is .data/sharadar/gics under the repository.
SHARADAR_GICS_DIR = "/absolute/path/to/dated/gics"
```

The command-line runner reads **environment variables**, not Streamlit secrets.
For example, enter the replacement without echoing it or saving it in shell history:

```bash
# Bash (macOS/Linux)
read -r -s -p "Sharadar API key: " SHARADAR_API_KEY; echo
export SHARADAR_API_KEY
python sharadar_backtest.py --check-access
```

On Windows PowerShell:

```powershell
$key = Read-Host "Sharadar API key" -AsSecureString
$env:SHARADAR_API_KEY = [System.Net.NetworkCredential]::new("", $key).Password
python sharadar_backtest.py --check-access
```

The access check queries small samples and required fields from all five tables;
it does not prove full historical coverage or correct delisting valuations.

Ticker batches are limited to 50 securities and 200 characters (including commas)
using the vendor identifiers, matching Sharadar's ticker-parameter limit.
The adapter has offline fixture tests. The user confirmed live access to the
first four tables on Windows; security metadata access and full historical
coverage must also be verified on a machine that can reach `api.sharadar.com`.

**No manual sector files are required by default.** The default configuration
uses Sharadar's `tickers` sector/industry labels, including for delisted companies.
These are **current vendor classifications, not point-in-time GICS**. They are
adapted into the pipeline's existing sector-column names, preserving the Energy
exclusion and sector-based portfolio labels, but remaining classification and
look-ahead bias must be disclosed. Missing labels/identifiers fail explicitly;
there is no substitute universe from Wikipedia.

For strict dated GICS instead, set `SHARADAR_SECTOR_SOURCE=dated_gics` in your
environment or private Streamlit secrets. For each selection date, supply
`.data/sharadar/gics/sp500_YYYY-MM-DD.csv` with columns:

```csv
Symbol,Security,GICS Sector,GICS Sub-Industry
```

Each file must contain the exact historical S&P 500 roster once, including
companies later removed or delisted. For portfolio year 2016, the input date is
2015-12-31. Use Sharadar's security identifiers, including its disambiguated
delisted tickers; period/hyphen share-class normalization is supported. Obtain
dated classifications from a licensed historical source. Matching the roster
does **not** establish that classifications were actually known on that date;
you remain responsible for provenance.

Once configured:

```bash
python sharadar_backtest.py --start-year 2016 --end-year 2025 --estimate-delisted
python -m streamlit run app.py
```

Alternatively, on **Backtest Results**, choose **Sharadar historical results**,
select the first and last portfolio years, and click **Generate Sharadar backtest**.
Generation is available even when no summary file exists. To allow estimated
exits, check **Estimate delisted exits at last close** in the sidebar before
generating. Otherwise missing terminal valuations stop the affected year.
This uses your inherited environment key or private Streamlit secrets and does
not require another terminal command. Re-generating replaces and archives
successful requested years. The page reports each unsuccessful year explicitly;
cumulative statistics are withheld if requested years failed or saved years have gaps.

Summary, detailed holdings, archives and selection traces are isolated under
`.data/sharadar/`, which is ignored by Git (the CLI writes selection traces;
both CLI and UI export classification and exit-policy disclosures). No baseline CSVs are overwritten,
no deployment is performed, and licensed source data must not be committed or
redistributed without permission. A lapsed subscription's permitted local
retention/use is governed by your vendor agreement.

Stock selection aggregates adjusted daily closes into Monday-labeled weekly
bars using only observations on or before the selection date. Sharadar forward
returns use actual closes on the last SPY trading day on or before each annual
boundary, rather than backfilling a start price from future observations.
Missing/nonpositive caps, missing classifications, malformed membership, and
unexplained missing endpoint prices fail explicitly; there is **no live-data fallback**.
The roster-size check rejects obvious partial snapshots but cannot prove
membership accuracy or historical coverage.

**This is not a fully point-in-time, verified-delisting-return study.** With
`--estimate-delisted` (or the UI checkbox), an early price termination is eligible
for estimation only when the security master marks it delisted and its exact
last price date matches the final observed adjusted close within the holding year.
The estimate liquidates at that close and holds cash at **0%** through year-end.
This is not the actual merger consideration or bankruptcy recovery and can
materially overstate bankrupt-stock returns. It does not automatically reinvest
in an acquirer or assume a verified payout. Ordinary missing quotes are not
treated as delistings.

CSV disclosures include `Sector_Source`, `Delisting_Policy`,
`Estimated_Exit_Count` (summary), and `Exit_Valuation` (holdings). CLI selection
traces also record the estimate notes. Strict mode remains available by omitting
the estimate flag/checkbox. Skipping incomplete years can itself bias results:
the CLI exits unsuccessfully when any requested year is absent. Verify complete
annual coverage and investigate estimated exits before drawing conclusions.
Existing reconciled CLI years are retained; to recalculate those years, use the
UI generation button or archive/move the isolated output files first.

### Email and acknowledgment data

- The email field is **optional**, self-reported, and unverified. It is not used
  for authentication, email delivery, or a mailing list.
- Email and accepted document versions are held only in Streamlit session state;
  this flow does not create account records, store consent timestamps, or write
  to SQLite/PostgreSQL. It provides **no durable consent audit trail**.
- A fresh session shows the cover sheet again. Ordinary reruns and navigation
  within a session do not. Changing `TERMS_VERSION`, `PRIVACY_VERSION`, or
  `LEGAL_VERSION` in `access_gate.py` requires opening the app again.
- The previous storage modules and schema remain for compatibility, but the
  cover sheet does not use them. Existing stored records are not deleted or
  migrated by this change; the owner must manage their retention separately.
- Hosting-provider logging is separate from session state and follows the
  provider's policies.

### Local verification

With the project's existing pytest framework installed:

```bash
python -m pytest -q test_access_gate.py
python -m pytest -q
```

Check that direct visits to either dashboard, Backtest Results, or How It Works
show the cover sheet first; all three documents and warnings are visible; blank
email permits entry; typing email alone does not; Open App permits entry; reruns
retain entry; and a fresh session or any legal-version change shows the cover
sheet again. No live deployment is included.

<details>
<summary>Legacy Google / database implementation reference — not required or active</summary>

The following documents the **previous authentication-gated flow**, not the
current cover sheet. Its configuration alone does **not** enable Google sign-in
or database recording in the app. It is retained only as reference for the unused
`identity_store.py` module and its database tests. Do not perform this setup to
use the current app.

### Configure Google OIDC

1. In Google Cloud, configure the OAuth consent screen for an **External**
   audience and create an OAuth client of type **Web application**. For local
   testing, add test users while the consent screen is in Testing status. To allow
   any Google account later, the owner must move it to Production and complete
   any Google-required review. Workspace policies can still restrict users.
2. Register the exact authorized redirect URI:
   `http://localhost:8501/oauth2callback`. Use only the basic
   `openid profile email` scopes; do not configure a hosted-domain restriction.
3. Create a private `.streamlit/secrets.toml` at the repository root with these
   keys (values below are placeholders, not credentials):

   ```toml
   [auth]
   redirect_uri = "http://localhost:8501/oauth2callback"
   cookie_secret = "REPLACE_WITH_A_LONG_RANDOM_SECRET"

   [auth.google]
   client_id = "REPLACE_WITH_GOOGLE_WEB_CLIENT_ID"
   client_secret = "REPLACE_WITH_GOOGLE_WEB_CLIENT_SECRET"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   client_kwargs = { scope = "openid profile email", prompt = "select_account" }
   ```

   Generate a cookie secret locally with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   Never commit this file or real values. The secrets file and default account
   database directory are ignored by Git. Do not enable token exposure.
4. Install and run from the repository root:

   ```bash
   python -m pip install -r requirements.txt
   python -m streamlit run app.py
   ```

   The alternate dashboard is `python -m streamlit run cb_taxable_portfolio_app.py`.
   Native authentication uses `st.login("google")`, `st.user`, and `st.logout()`;
   Streamlit/Authlib handle OIDC signature, state, and nonce validation. Do not
   substitute query parameters or manually supplied email addresses for `st.user`.
   With no auth configuration, the sign-in screen still renders but sign-in
   reports a configuration error and protected features remain blocked.

For a **future, separately authorized deployment**, register the exact HTTPS
`https://YOUR_APP_HOST/oauth2callback` URI in Google and configure the same URI and
secret keys in the host's secret manager. Keep CORS/XSRF protection enabled.
Streamlit's identity cookie normally lasts up to 30 days; signing out clears
the application's cookie, not the user's Google session or other already-open
app sessions. Close other tabs on shared devices. Do not assume that signing out
deletes account or consent records.

### Recorded identity and consent data

`identity_store.IdentityStore` (SQLite) and `PostgresIdentityStore` share account and
consent behavior behind `create_identity_store`. SQLite is initialized idempotently
on authenticated startup. PostgreSQL validates the pre-provisioned schema on startup
without needing schema-owner privileges. The default local database
is `.data/identity.sqlite3`, resolved relative to the repository, not the working
directory. Set **`CB_IDENTITY_DB`** to an absolute path to use another database file.

- `users`: internal integer ID, provider (`google`), canonical issuer and Google
  subject (unique together), email and nullable email-verification indicator,
  and UTC first/latest observed sign-in timestamps. Email is **not** an account
  key. Each new authenticated Streamlit session (including cookie-restored
  sessions), or changed token issuance time, updates latest sign-in; ordinary
  widget reruns do not. These are application-observed times, not a complete
  audit log of Google login events.
- `legal_acknowledgments`: append-only acceptance events with an internal ID,
  user foreign key, UTC acceptance time, and all three document versions.
  The database is checked on every page run; consent is not authorized by a
  session-state flag. Each checkbox is scoped to account and document versions.
- No Google passwords, OAuth tokens, IP addresses, or user agents are stored in
  these tables. Retention is owner-managed; contact `wmrcamp@gmail.com` for
  privacy/access/deletion requests. Treat database files and backups as private
  personal data: restrict filesystem access and encrypt backups as appropriate.

SQLite is durable only on a persistent, writable disk. Ephemeral hosting disks
(including typical Streamlit Community Cloud storage) are **not** a durable
production account store. Use the PostgreSQL backend below for Neon/Community Cloud,
or arrange persistent disk storage/backups for a single-instance SQLite host.
Do not place a SQLite database in a public/static directory or commit it.
Unavailable storage blocks access; there is no session-only consent fallback.
Future schema changes require explicit migrations; the new tables/index are
created idempotently without modifying the existing portfolio CSVs.

### Neon PostgreSQL and Streamlit Community Cloud

This backend uses Psycopg 3, not Neon Auth, Functions, the Data API, or a JavaScript
build. No Neon CLI/MCP setup is required. Nothing in this change provisions cloud
resources, changes live secrets, or deploys the app.

1. In your existing Neon project, select the intended branch and database.
   Use a disposable development branch first, not production account records.
   Keep all passwords and full connection strings private.
2. As database owner, run `docs/identity_postgres.sql` in Neon's SQL Editor with
   the intended branch/database selected. Alternatively, from the repository root,
   use `psql "$NEON_ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 -f docs/identity_postgres.sql`
   with that private environment variable set outside shell history.
   This transaction creates the dedicated `cb_identity` schema, tables, and index
   idempotently, serializes concurrent initialization, and removes PUBLIC access.
   It does not copy or delete existing SQLite records or portfolio data.
3. Create a **restricted login role**, not a database owner, for app runtime.
   For example, an administrator can run the following once in the SQL Editor:

   ```sql
   CREATE ROLE cb_identity_app LOGIN;
   GRANT CONNECT ON DATABASE neondb TO cb_identity_app;
   GRANT USAGE ON SCHEMA cb_identity TO cb_identity_app;
   GRANT SELECT, INSERT, UPDATE ON cb_identity.users TO cb_identity_app;
   GRANT SELECT, INSERT ON cb_identity.legal_acknowledgments TO cb_identity_app;
   GRANT USAGE ON ALL SEQUENCES IN SCHEMA cb_identity TO cb_identity_app;
   ```

   Replace the database name if yours differs. Set a strong role password through
   a private administrator connection using psql's `\password cb_identity_app`
   prompt (or your provider's secure role-password interface). Do not put passwords
   in committed SQL, this chat, screenshots, or shell history.
   This role must not inherit `neon_superuser`, schema ownership, or other privileged
   roles. Do not use `neondb_owner` as the runtime credential. The role can insert
   acknowledgments, but cannot update/delete them or create/drop application tables.
   Schema changes remain an administrator task; rerun the schema file as owner,
   not through the restricted app role.
4. Obtain the **PostgreSQL URL** for that restricted role from Neon Connect.
   A pooled connection endpoint is supported. Copy the raw URL beginning
   `postgresql://` or `postgres://`, not the `psql` command or surrounding quotes.
   Keep Neon's TLS connection parameters. The app always upgrades TLS to
   `sslmode=verify-full` and uses system trusted roots by default, even if the URL
   says `sslmode=require`. Explicit insecure modes are rejected. A private CA can
   be configured with `sslrootcert` when needed; certificate verification is never
   disabled. Connections have a 10-second connection timeout, a 15-second statement
   timeout, and a 5-second lock timeout.
5. When separately authorized to configure your host, append this table to
   **Streamlit app settings → Secrets**, preserving the existing `[auth]` and
   `[auth.google]` tables. Replace the placeholder **only in the private editor**:

   ```toml
   [identity]
   backend = "postgresql"
   database_url = "REPLACE_WITH_PRIVATE_RESTRICTED_ROLE_POSTGRESQL_URL"
   ```

   The Google `redirect_uri` for the known app is
   `https://cb-tax-exempt-protocol.streamlit.app/oauth2callback`.
   Saving host secrets can restart the app; do not do so until you intend to test
   that running instance. Never commit real secrets, `.env` files, database URLs,
   or downloaded Neon/Google credential files.

For local development, the equivalent environment settings are
**`CB_IDENTITY_BACKEND=postgresql`** and **`CB_IDENTITY_DATABASE_URL`** containing
the private URL. Environment values override the corresponding `[identity]` keys.
Providing a database URL also selects PostgreSQL automatically; explicitly setting
`backend = "postgresql"` makes a missing URL fail closed. Empty/invalid URLs,
missing schemas, permission failures, connection failures, and failed consent writes
block access: they **never** fall back to SQLite or session-only consent. Neither
raw database URLs nor driver errors are shown in the UI or stored in widget keys.
Remove the PostgreSQL configuration to return to local SQLite; `CB_IDENTITY_DB`
is still the SQLite file path. Existing SQLite records are **not** auto-migrated:
switching to an empty PostgreSQL store requires users to accept again.

Before public use, verify backup/restore and retention options for your Neon plan;
do not assume a free plan provides your required recovery window. Test sign-in and
consent against a private test database, restart the app, verify consent is retained,
and confirm the restricted role cannot alter/delete acknowledgment history.
Real Google OAuth and live Neon credentials must be verified by the owner privately.

Legal wording lives in `access_gate.py`, with personal ownership:
**© 2026 William R. Camp. All rights reserved.** Contact: **wmrcamp@gmail.com**.
Have the owner/legal adviser review the notices before production use. Update
`TERMS_VERSION`, `PRIVACY_VERSION`, or `LEGAL_VERSION` whenever the corresponding
document changes materially. A changed version requires all three acknowledgments
again; prior events are preserved. Use new, unique versions rather than reusing
old labels. The current documents can always be reviewed in the app.

### Local verification

Offline tests use temporary databases and mocked Streamlit identities; they do
not contact Google or deploy:

```bash
python -m pytest -q test_identity_store.py test_access_gate.py test_postgres_identity_store.py
python -m pytest -q
```

Real PostgreSQL integration tests are opt-in: privately set
`CB_TEST_POSTGRES_URL` to an **administrator URL for a disposable PostgreSQL test
database**, then run `python -m pytest -q test_postgres_identity_store.py`.
They provision the schema twice, create/remove a temporary restricted role, and
leave synthetic account/consent rows in that disposable database. They check verified
TLS, account upserts, optional claims, per-version consent, concurrency, transaction
rollback, and restricted-role permissions.
Never point this variable at production. Without it, these real-server tests
are skipped; driver/configuration and SQLite tests still run offline.

</details>

## Market data and the performance gate

**Market caps.** The live provider fetches each screened candidate's Yahoo Finance
`marketCap` (current values, even for earlier As of Dates) with up to 3 attempts per
ticker and exponential backoff, using 8 parallel workers. Only finite, positive
numbers are valid; a missing, zero, negative, non-numeric, NaN or infinite cap is
assigned zero and ranked last, with a warning identifying affected tickers.
Every candidate is attempted, even if earlier requests fail. Ties preserve constituent
order; if all caps are unavailable, that order determines the selected universe.
This intentionally permits portfolios with incomplete market-cap ranking.
Batches, including zero placeholders, are cached for 24 hours.
`protocol_engine.select_universe` applies the same normalization to every provider,
including injected ones. Historical snapshot audits
(`snapshot_audit.py`) keep their own structural checks.

**Refresh market data.** The sidebar button clears only the cached S&P 500
constituents, market caps and SPY benchmark returns (`data_fetcher.clear_market_data_caches`),
so the next run refetches them; access and identity state are untouched.

**Performance gate modes** (`portfolio_pipeline.run_pipeline`'s `gate_threshold`):

- `BENCHMARK_GATE` (dashboard default): the threshold is SPY's trailing-12-month
  return computed in the same run, from the same weekly price download, fill rules
  and 53-close window ending at the As of Date as every stock's `T12M_Return`.
- A finite decimal number: a fixed/custom threshold applied exactly (dashboard
  "Custom threshold"). New backtest runs use `BENCHMARK_GATE` too, not a separately
  fetched benchmark threshold. Existing saved backtest CSVs have not been regenerated
  and reflect their original protocol rules.
- `None`: gate off.

After each run the dashboards show the As of Date, the applied threshold, its source
and the weekly window, plus market-cap coverage; `SelectionTrace` records
`gate_mode`, `gate_threshold` and `benchmark_window_start`/`_end`. A missing SPY
series or non-finite threshold raises `BenchmarkError`; it is never treated as 0% or
as gate off. Data failures raise `data_errors.MarketDataError` subclasses with trace
status `market_cap_failed` (empty universe), `benchmark_failed` or `market_data_failed`; the
dashboards display them as errors with no portfolio.

## Final portfolio selection

Shadow-list tickers retain first priority, ordered by ascending downside beta.
For each factor, non-shadow candidates must have an absolute loading of at least
0.59 on that specific factor (including negative loadings of qualifying magnitude).
Eligible candidates are ranked by trailing-12-month return (`T12M_Return`),
highest first, then maximum absolute factor loading descending, downside beta
ascending, and ticker ascending to resolve ties deterministically. Missing,
invalid, or non-finite returns rank after all finite returns.

Remaining slots use the same return-first ranking for non-shadow candidates,
requiring a qualifying loading on at least one factor. Selection keeps the
existing screening gates, shadow priority, maximum of three holdings per dominant
factor, unique tickers, target-size limit, and equal weights. Selection uses only
trailing returns, not the subsequent holding-period returns measured by backtests.

## Backtest CSV exports

### One yearly calculation for both CSVs

Each backtest year is calculated once by `backtest_engine.compute_backtest_year`,
which returns a `YearlyBacktest` holding the annual summary row and the detailed
ticker rows together. Selection (current rules, 104-week downside-beta lookback),
the weekly holding-year start/end prices, each ticker's return, the equal-weight
portfolio return, the SPY benchmark return and alpha are computed once, and both
outputs are built from those values. Before a year is published,
`reconcile_year` checks that:

- every ticker return equals `End_Price / Start_Price - 1`;
- weights are equal 1/N fractions summing to 1;
- the weighted sum of ticker returns equals the summary `Portfolio_Return`;
- every row's `Portfolio_Return`, `Benchmark_Return` and `Alpha` equal the summary,
  with `Alpha = Portfolio_Return - Benchmark_Return`;

all within an absolute tolerance of `RECONCILIATION_TOLERANCE` (1e-9). A holding
with no holding-year prices fails the year (trace status `missing_prices`) instead of
silently dropping out of the average. Detailed rows are never reconstructed
independently of their summary row.

### Hosted app (no terminal required)

1. Open **Backtest Results**. **Download annual summary CSV** downloads the summary
   file bytes (including any source rows excluded from the completed-year view).
2. Detailed holdings are published only for years whose rows reconcile with the
   displayed summary row; mismatched years are listed with the reason. Click
   **Rebuild summary and detailed holdings for unreconciled years** to run the yearly
   calculation once for each missing or mismatched completed year. That year's
   summary row and holdings rows are replaced together; replaced rows are appended
   to `backtest_results_archive.csv` / `backtest_detailed_results_archive.csv`
   with an `Archived_At` timestamp. Rendering the page and clicking downloads do not
   run selection.
3. Download the detailed CSV and review its preview. Partial downloads are labeled
   and missing years listed. Retry after a provider failure; successful years are
   retained and are not regenerated on reruns.
4. Upload either CSV using Google Sheets' **File → Import → Upload**.

Rebuilt results are saved locally when possible and retained in the current
Streamlit session across reruns. On read-only storage, a warning explains that the
downloads are session-only: download them before closing the session. Session data
is not durable across disconnects, server restarts, or redeployments.
An absent/invalid/empty summary cannot establish which historical years to rebuild,
so it does not offer rebuilding or an empty download.

### Command-line history updates

Run `python backtest_engine.py` to generate
`backtest_results.csv` (annual summary) and `backtest_detailed_results.csv`
(one row per selected ticker per year). The default window starts in 2016 and
ends at the last completed year: 2025 when run in 2026. Years whose existing
summary and detail rows already reconcile are left untouched; every other year in
the window is recalculated once and both files are written from that result, with
replaced rows archived as above. Default data paths are relative to the
module/repository root, not the working directory; explicit paths remain supported.

For an explicitly fixed 2016–2025 window or custom output paths:

```python
from backtest_engine import update_backtest_history

update_backtest_history(
    csv_path="backtest_results.csv",
    detailed_csv_path="backtest_detailed_results.csv",
    start_year=2016,
    end_year=2025,
)
```

Detailed headings are `Year`, `Ticker`, `Weight`, `Weight_Percent`, `Start_Price`,
`End_Price`, `Annual_Return`, `Portfolio_Return`, `Benchmark_Return`, and `Alpha`.
The hosted export also includes `Detail_Source`: rows rebuilt in the app are marked
as generated with their annual summary row, while existing rows without provenance
are labeled unverified (they are published only if they reconcile).
Weights are exact equal-weight fractions (1/N), not rounded display weights.
Returns are decimal fractions (0.10 means 10%); `Weight_Percent` uses percent units.
Prices and annual returns use backfilled/forward-filled weekly close endpoints.

The default live provider uses current constituents and market caps, even for
historical dates, so a rebuilt year describes a newly calculated backtest, not the
original historical holdings; rebuilt summary returns may differ from archived ones.

### Verification and deployment

Offline workflow tests use Streamlit AppTest with a deterministic mocked provider
and mocked factor fitting, exercising the button, selection, pricing, export,
reruns, failures, and read-only fallback. They do not validate live data quality
or a deployed app. Run with the existing Python requirements and pytest:

```bash
python -m pytest -q test_backtest_workflow.py test_backtest_exports.py test_weekly_sampling.py test_protocol_engine.py test_selection_trace.py test_market_data.py
python -m pytest -q
```

After deployment, verify the page against the configured live provider, check
provider rate limits/data availability and progress/error reporting, download both
CSVs, and confirm published holdings reconcile with the summary. No live-provider or
deployment success is implied by the offline tests.
