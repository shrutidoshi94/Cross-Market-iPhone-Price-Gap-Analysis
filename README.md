# Cross-Market iPhone Price Gap Analysis

Track the price of a single SKU — **iPhone 17 Pro Max, 512GB, Cosmic Orange** — across eight retail markets over time, normalize everything to USD, and surface which country looks cheapest after rough shipping/duty friction.

This is a personal data-science portfolio project: a small collect → store → feature → dashboard pipeline you can run locally.

---

## Problem framing

Identical consumer electronics rarely cost the same worldwide. Gaps come from taxes, currency moves, channel strategy, and local competition. For a few products those gaps are large enough that people (and grey-market resellers) treat them as **arbitrage**.

For analysts and retailers, the same gaps are a window into **international pricing strategy**:
- Is a market systematically expensive after FX?
- Do prices move together, or does one country lead?
- After realistic friction (shipping, duty), does a “cheap” shelf price still look attractive?

This project answers a concrete version of that question for one flagship iPhone SKU, with a self-built history of hourly snapshots.

---

## Data source and limitations

| Piece | Source |
|-------|--------|
| Product prices | [PricesAPI.io](https://pricesapi.io/) product search |
| FX rates | [exchangerate.host](https://exchangerate.host) (APILayer) daily historical rates |
| Storage | Local SQLite (`data/prices.db`) |

**Important limitations**

1. **Snapshot-based history** — PricesAPI returns *current* matches, not a long historical series. The time series in this repo is whatever we collect ourselves (GitHub Actions every 12 hours, or local `scheduler.py`). Early charts will look sparse until a few days of snapshots exist.
2. **Search matching** — Queries are free-text, but collectors **reject** any listing whose title is not clearly **iPhone 17 Pro Max 512GB** (other storages and models are dropped). Cosmic Orange is preferred when available. Each kept row stores a retailer **source page URL** for verification. Prices outside per-currency sanity bands are treated as outliers and ignored.
3. **VAT / sales tax** — Shelf prices may include or exclude tax depending on country and retailer. Cross-market “savings” can be overstated or understated.
4. **Duty & shipping are assumptions** — `arbitrage_score` subtracts rough friction from `features.py` (`DUTY_ESTIMATES`, `SHIPPING_ESTIMATES_USD`). These are **not** customs quotes; they exist to stop a naïve “cheapest sticker price wins” conclusion. Tune them before trusting the callout.
5. **Rate limits** — PricesAPI Personal tier is tight (credits + per-minute caps). The project rotates up to five API keys and backs off on HTTP 429.

### How duty / shipping estimates were derived

Values in `features.py` are **order-of-magnitude** assumptions for a US-based buyer:

| Country | Duty / tax friction | Shipping (USD) | Rationale (rough) |
|---------|---------------------|----------------|-------------------|
| US | 0% | $0 | Baseline / buy local |
| GB, DE, FR | 0% | $40–45 | VAT usually in shelf price; modest outbound shipping |
| CA, AU | 5% | $25–55 | Light brokerage / import buffer |
| JP | 8% | $50 | Consumption-tax-style buffer + shipping |
| IN | 20% | $60 | Phones often face high effective import friction into / out of India |

Re-tune these constants if you have better landed-cost data. The dashboard disclaimer links back here on purpose.

---

## Architecture

```
                    ┌─────────────────────┐
                    │  PricesAPI.io keys  │
                    │  (round-robin)      │
                    └─────────┬───────────┘
                              │
                              ▼
┌──────────────┐      ┌──────────────┐      ┌─────────────────┐
│ scheduler.py │─────▶│  collect.py  │─────▶│ SQLite           │
│ (hourly /    │      │  per-country │      │ price_snapshots  │
│  --once)     │      │  search      │      └────────┬────────┘
└──────────────┘      └──────────────┘               │
                                                     │
                              ┌──────────────┐       │
                              │    fx.py     │◀──────┤  (normalize)
                              │ exchangerate │       │
                              │ host → cache │       │
                              └──────┬───────┘       │
                                     │               │
                                     ▼               ▼
                              ┌─────────────────────────┐
                              │      features.py        │
                              │  price_usd, gaps,        │
                              │  rolling avg, score     │
                              └───────────┬─────────────┘
                                          │
                                          ▼
                              ┌─────────────────────────┐
                              │   Streamlit app.py      │
                              │   charts + best market  │
                              └─────────────────────────┘
```

| Module | Role |
|--------|------|
| `config.py` | `.env` keys, country list, `KeyRotator` (10 req/min per key + 429 backoff) |
| `collect.py` | Query PricesAPI per country; append rows to `price_snapshots` |
| `fx.py` | Fetch/cache FX into `fx_rates`; `normalize_to_usd()` |
| `features.py` | Analysis DataFrame: gaps vs US, rolling avg, arbitrage score |
| `scheduler.py` | Local loop (`schedule`) or `--once`; CI uses `--once` twice daily |
| `app.py` | Streamlit + Plotly dashboard |

---

## How to run it

### 1. Prerequisites

- Python 3.11+
- PricesAPI.io API keys (up to 5 for rotation)
- An [exchangerate.host](https://exchangerate.host) / APILayer access key

### 2. Setup

```bash
git clone <your-fork-url>
cd "Cross-Market iPhone Price Gap Analysis"

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set PRICESAPI_KEY_1..5 and EXCHANGERATE_HOST_ACCESS_KEY
```

### 3. Collect a test snapshot

```bash
# Single country (cheap smoke test)
python collect.py --once --country US

# Or via the scheduler entrypoint
python scheduler.py --once --country US

# Full 8-country cycle (uses more API credits; cold calls can take ~30–90s each)
python scheduler.py --once
```

### 4. Scheduled collection on GitHub Actions (recommended)

The workflow [`.github/workflows/collect.yml`](.github/workflows/collect.yml) runs **twice a day** (00:00 and 12:00 UTC), appends rows to `data/prices.db`, and commits the DB back to the repo so history survives.

**Free-tier budget (why 12 hours, not hourly):**

| Limit | Free allowance | This workflow (~30 days) |
|-------|----------------|---------------------------|
| PricesAPI credits | 5 keys × 1,000 = 5,000/mo | 8 countries × 2/day × 30 ≈ **480** |
| Actions minutes (private) | 2,000/mo | ~60 jobs × 10–15 min ≈ **600–900** |
| GitHub repo storage | soft multi‑GB scale | SQLite stays **KB–low MB** |

Do **not** also run local hourly `scheduler.py` while Actions is enabled — that doubles credit use.

**One-time secrets setup** (repo → Settings → Secrets and variables → Actions):

- `PRICESAPI_KEY_1` … `PRICESAPI_KEY_5`
- `EXCHANGERATE_HOST_ACCESS_KEY`

Then open the **Actions** tab → **Collect prices** → **Run workflow** for a manual test.

```bash
# Optional: keep collecting on your laptop instead of Actions
python scheduler.py
```

### 5. Open the dashboard

```bash
streamlit run app.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

To host for free: [Streamlit Community Cloud](https://share.streamlit.io) → **New app** → pick this repo → **Main file path:** `streamlit_app.py`.

Add secrets (App settings → Secrets):

```toml
PRICESAPI_KEY_1 = "..."
PRICESAPI_KEY_2 = "..."
PRICESAPI_KEY_3 = "..."
PRICESAPI_KEY_4 = "..."
PRICESAPI_KEY_5 = "..."
EXCHANGERATE_HOST_ACCESS_KEY = "..."
```

Free Streamlit Cloud works most easily with a **public** repo (this one is private today — Settings → Change visibility if you want to deploy). The dashboard reads `data/prices.db` from the repo, which Actions updates twice daily.

### Useful one-liners

```bash
python config.py          # verify API keys load (never prints secrets)
python fx.py              # fetch/cache today's FX rates
python features.py        # print the feature table to the terminal
```

---

## Key findings

> **[TODO: fill in after a few days of data collection]**
>
> Once Actions has built a multi-day history, summarize here:
> - Which market is cheapest on average (pre- and post-friction)?
> - How stable is the ranking across snapshots?
> - Which market shows the highest USD price volatility?
> - Any FX-driven moves that flip the “best market” callout?

---

## Project layout

```
.
├── .env.example          # Key placeholders (copy to .env — never commit .env)
├── .github/workflows/
│   └── collect.yml       # Twice-daily collection + DB commit
├── requirements.txt
├── config.py             # Keys, countries, KeyRotator
├── collect.py            # PricesAPI → SQLite
├── fx.py                 # FX fetch + normalize_to_usd
├── features.py           # Analysis-ready DataFrame
├── scheduler.py          # Local hourly / --once runner
├── app.py                # Streamlit dashboard logic
├── streamlit_app.py      # Community Cloud entry point
└── data/
    └── prices.db         # Snapshot history (tracked for Actions persistence)
```

---

## License

Personal portfolio project — use and adapt freely for learning and demos.
