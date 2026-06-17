# MEIC Trader

Automated Multiple Entry Iron Condor (MEIC) paper trading bot for XSP using the
[tastytrade](https://tastytrade.com) API. Places up to 4 iron condor entries per
day at scheduled times, manages GTC stop-limit orders, and tracks P&L in a local
SQLite database.

---

## Features

- **4 daily IC entries** at configurable ET times (default 10:55, 11:25, 12:25, 13:35)
- **Delta-targeted strikes** — sells closest to target delta without exceeding it
- **GTC stop-limit orders** placed automatically after each fill (90% trigger / 95% limit)
- **EOD report** auto-generated at 16:05 ET with rich terminal table + CSV
- **Sandbox and live modes** — same codebase, separate credential stores
- **Live safety guards** — debit check, credit range check, buying power check
- **Named strategy profiles** stored in `profiles/` — switch with one command
- **Windows Credential Manager** storage for all credentials (no `.env` files)

---

## Requirements

- Python 3.11+
- tastytrade sandbox or live account
- Windows (credential storage uses DPAPI via `keyring`)

Install dependencies:

```
pip install -r requirements.txt
```

---

## Initial Setup

### 1. Store credentials

Credentials are stored in Windows Credential Manager — never in files.

```
python meic.py secrets              # store sandbox credentials (default)
python meic.py secrets --live       # store live credentials
```

You will be prompted for:
- **Client secret** — from your tastytrade developer portal
- **Refresh token** — OAuth refresh token from tastytrade

After storing, the credentials are immediately verified with a test login. If
verification fails the credentials are removed automatically.

To check what is stored:

```
python meic.py secrets --show
```

To remove stored credentials:

```
python meic.py secrets --delete          # remove sandbox credentials
python meic.py secrets --delete --live   # remove live credentials
```

### 2. Create a profile

On first run the interactive setup wizard launches automatically. To create a
profile manually:

```
python meic.py config new
python meic.py config new --profile conservative
```

You will be prompted for all settings (see [Profile Settings](#profile-settings)
below). Defaults are shown in brackets.

### 3. Test the connection

```
python meic.py auth           # sandbox (default)
python meic.py auth --live    # live account
```

Prints a connection banner showing account number, net liquidating value, buying
power, and token expiry. Exits immediately — does not start the scheduler.

### 4. Run the bot

```
python meic.py run            # sandbox (default)
python meic.py run --live     # live account — orders submitted to market
```

---

## CLI Reference

### `meic.py run`

Start the trading bot and scheduler.

```
python meic.py run [--sandbox | --live] [--profile NAME]
```

| Flag | Description |
|---|---|
| `--sandbox` | Use sandbox account (default) |
| `--live` | Use live account — all orders submitted to market |
| `--profile NAME` | Override the active profile for this session |

### `meic.py auth`

Test the connection and print the startup banner. Does not start the scheduler.

```
python meic.py auth [--sandbox | --live] [--profile NAME]
```

### `meic.py report`

Display P&L and win-rate tables for various timeframes.

```
python meic.py report <timeframe> [options]
```

| Timeframe | Example |
|---|---|
| `daily` | `python meic.py report daily` |
| `daily --date YYYY-MM-DD` | `python meic.py report daily --date 2026-06-13` |
| `weekly` | `python meic.py report weekly` |
| `weekly --date YYYY-MM-DD` | `python meic.py report weekly --date 2026-06-09` |
| `monthly` | `python meic.py report monthly` |
| `yearly` | `python meic.py report yearly` |
| `all-time` | `python meic.py report all-time` |
| `custom` | `python meic.py report custom --from 2026-06-01 --to 2026-06-16` |

Additional flags:

| Flag | Description |
|---|---|
| `--sandbox` / `--live` | Which database table to read from (default: sandbox) |
| `--experimental` | Read from the experimental trades table |
| `--no-csv` | Suppress CSV file output |
| `--csv PATH` | Write CSV to a custom path |

Today's report reads live data from the tastytrade API. Past dates read from the
local database.

### `meic.py secrets`

Manage credentials in Windows Credential Manager.

```
python meic.py secrets              # store sandbox credentials (prompts)
python meic.py secrets --live       # store live credentials (prompts)
python meic.py secrets --show       # show which credentials are stored
python meic.py secrets --delete     # delete sandbox credentials
python meic.py secrets --delete --live  # delete live credentials
```

### `meic.py config`

Manage strategy profiles.

```
python meic.py config list                       # list all profiles
python meic.py config show                       # show active profile
python meic.py config show --profile NAME        # show a specific profile
python meic.py config new                        # interactive TUI — create a profile
python meic.py config new --profile conservative # create a named profile
python meic.py config switch --profile NAME      # set active profile
python meic.py config set --delta 0.12           # update a field in the active profile
python meic.py config delete --profile NAME      # delete a profile
```

`config set` accepts any combination of:

```
--delta FLOAT       --width FLOAT       --symbol STR
--quantity INT      --min-credit FLOAT  --max-credit FLOAT
--bp-buffer FLOAT   --profile NAME
```

---

## Profile Settings

Each profile is stored as a JSON file in `profiles/<name>.json`. A profile
contains all strategy and safety parameters for one trading configuration.

| Field | Default | Description |
|---|---|---|
| `symbol` | `XSP` | Underlying symbol |
| `delta` | `0.15` | Target short strike delta (≤ this value) |
| `wing_width` | `3.0` | Points between short and long strikes |
| `entry_times` | `["10:55","11:25","12:25","13:35"]` | ET entry times |
| `quantity` | `1` | Contracts per entry |
| `stop_trigger_ratio` | `0.90` | Stop trigger as fraction of IC credit |
| `stop_limit_ratio` | `0.95` | Stop limit as fraction of IC credit |
| `min_credit` | `0.50` | Minimum IC credit to accept (live guard) |
| `max_credit` | `5.00` | Maximum IC credit to accept (live guard) |
| `bp_check_enabled` | `true` | Enable buying power pre-flight check |
| `bp_buffer` | `1.25` | BP headroom multiplier (1.0 = no buffer) |
| `experimental` | `false` | Enable experimental mode (see below) |

### Validation

Standard profiles enforce these limits:

| Field | Range |
|---|---|
| `delta` | 0.05 – 0.30 |
| `wing_width` | 1 – 10 |
| `symbol` | Must exist in tastytrade option chains |

### Experimental Mode

Setting `experimental: true` on a profile:

- Bypasses delta and wing width validation limits (warns instead of errors)
- Shows `⚠ EXPERIMENTAL PROFILE` in the startup banner
- Routes all trades to the `experimental_ic_entries` database table, keeping them
  separate from real P&L history
- Requires `--experimental` flag on `meic.py report` to view

---

## Live Mode Safety Guards

When running with `--live`, three pre-flight checks run before each IC order is
submitted. Any failure skips the entry, logs a warning, and prints a bold red
notice to the console.

| Guard | Check |
|---|---|
| **Debit check** | IC mid price must be > 0 (a credit) |
| **Credit range** | IC credit must be within `[min_credit, max_credit]` |
| **Buying power** | `(wing_width × 100 − credit) × bp_buffer ≤ available_bp` |

Set `bp_check_enabled: false` in the profile to disable the BP check (useful for
sandbox where buying power is artificial).

---

## Database

Trades are stored in `trades.db` (SQLite, created automatically).

| Table | Contents |
|---|---|
| `sandbox_ic_entries` | Sandbox paper trades |
| `live_ic_entries` | Live brokerage trades |
| `experimental_ic_entries` | Trades from experimental profiles |

The EOD report (fired at 16:05 ET) reconstructs the day's trades from the API
and upserts them into the appropriate table.

---

## File Layout

```
meic_trader/
├── profiles/          # strategy profiles (one JSON file each)
├── reports/           # auto-generated CSV reports
├── trades.db          # SQLite trade history (auto-created)
├── meic.py            # CLI entry point
├── main.py            # startup, connection banner, scheduler wiring
├── scheduler.py       # time-based entry + EOD trigger
├── strategy.py        # entry orchestration
├── chain.py           # option chain + strike selection
├── orders.py          # IC order placement + stop-limit orders
├── client.py          # tastytrade session management + keepalive
├── config.py          # runtime config loader (keyring + profiles)
├── database.py        # SQLite init, upsert, query
├── report.py          # API reconstruction, rich tables, CSV
├── logger.py          # ET-timestamped logger
└── requirements.txt
```

Files excluded from version control (see `.gitignore`):

- `trades.db` — local trade history
- `profiles/` — personal strategy settings
- `settings.json` — active profile pointer
- `session_cache.json` — cached OAuth session token
- `reports/` — generated CSVs
