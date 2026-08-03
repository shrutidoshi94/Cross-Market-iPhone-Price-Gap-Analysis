"""
Feature engineering for cross-market iPhone price analysis.

Reads raw ``price_snapshots`` from SQLite and returns an analysis-ready
DataFrame with USD prices, gaps vs. the US baseline, time features,
rolling averages, and a simple arbitrage score.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from collect import is_plausible_price, is_target_sku
from config import DATABASE_PATH
from fx import ensure_rates_for_dates, normalize_to_usd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rough landed-cost assumptions (NOT real customs quotes).
# duty_fraction: estimated import duty / VAT friction as a fraction of price
# shipping_usd: flat estimated cross-border shipping + handling to the buyer
# These exist so arbitrage_score ≈ price_gap_usd - estimated friction.
# See README for methodology notes.
# ---------------------------------------------------------------------------
DUTY_ESTIMATES = {
    # country: duty / tax friction as a fraction of local price
    "US": 0.00,   # baseline market — buy locally
    "GB": 0.00,   # UK VAT usually already in shelf price; no extra US duty assumed for outbound
    "DE": 0.00,   # same note as GB for EU shelf prices
    "FR": 0.00,
    "CA": 0.05,   # rough brokerage / duty buffer when shipping CA → US buyer
    "AU": 0.05,
    "JP": 0.08,
    "IN": 0.20,   # India often has high effective phone import friction
}

SHIPPING_ESTIMATES_USD = {
    # Flat shipping + handling estimate to get the device to a US-based buyer
    "US": 0.0,
    "GB": 40.0,
    "DE": 45.0,
    "FR": 45.0,
    "CA": 25.0,
    "AU": 55.0,
    "JP": 50.0,
    "IN": 60.0,
}

# Match a non-US row to the nearest US snapshot within this window
US_MATCH_TOLERANCE = pd.Timedelta(hours=2)


def _db_path(db_path: Optional[Path] = None) -> Path:
    """Resolve the SQLite path relative to the project root when needed."""
    if db_path is not None:
        return Path(db_path)
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def load_price_snapshots(db_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Load the raw ``price_snapshots`` table into a DataFrame.

    Returns:
        DataFrame with at least country, retailer, price, currency, timestamp.
    """
    path = _db_path(db_path)
    if not path.exists():
        logger.warning("Database not found at %s", path)
        return pd.DataFrame()

    with sqlite3.connect(path) as conn:
        # source_url may be absent on very old DBs — select * then normalize
        df = pd.read_sql_query(
            "SELECT * FROM price_snapshots ORDER BY timestamp ASC, country ASC",
            conn,
        )

    if df.empty:
        return df

    if "source_url" not in df.columns:
        df["source_url"] = None

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    df["country"] = df["country"].str.upper()
    df["currency"] = df["currency"].str.upper()

    # Defense in depth: drop rows that are not 512GB Pro Max or are price outliers
    before = len(df)
    sku_ok = df["title"].map(is_target_sku)
    price_ok = [
        is_plausible_price(price, currency)
        for price, currency in zip(df["price"], df["currency"])
    ]
    df = df.loc[sku_ok & price_ok].copy()
    dropped = before - len(df)
    if dropped:
        logger.info("Dropped %d unclean snapshot row(s) during load", dropped)
    return df


def _add_price_usd(df: pd.DataFrame, db_path: Optional[Path] = None) -> pd.DataFrame:
    """Attach ``price_usd`` using fx.normalize_to_usd for each row."""
    if df.empty:
        df = df.copy()
        df["price_usd"] = pd.Series(dtype="float64")
        return df

    ensure_rates_for_dates(df["timestamp"].tolist(), db_path=db_path)

    prices_usd = []
    for row in df.itertuples(index=False):
        usd = normalize_to_usd(row.price, row.currency, row.timestamp, db_path=db_path)
        prices_usd.append(usd)

    out = df.copy()
    out["price_usd"] = prices_usd
    missing = out["price_usd"].isna().sum()
    if missing:
        logger.warning("%d row(s) missing price_usd after FX normalization", missing)
    return out


def _add_us_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add ``price_gap_usd`` and ``price_gap_pct`` vs. the nearest US price.

    US is the baseline market. Gap is defined as:
        foreign_price_usd - us_price_usd
    so negative values mean the foreign market is cheaper than the US.
    """
    out = df.copy()
    out["price_gap_usd"] = pd.NA
    out["price_gap_pct"] = pd.NA

    if out.empty or out["price_usd"].isna().all():
        return out

    us = (
        out.loc[out["country"] == "US", ["timestamp", "price_usd"]]
        .dropna(subset=["price_usd"])
        .sort_values("timestamp")
        .rename(columns={"price_usd": "us_price_usd", "timestamp": "us_timestamp"})
    )
    if us.empty:
        logger.warning("No US baseline rows — price gaps left null")
        return out

    # merge_asof needs sorted keys; keep original index to write gaps back
    foreign = out.reset_index().sort_values("timestamp")
    merged = pd.merge_asof(
        foreign,
        us,
        left_on="timestamp",
        right_on="us_timestamp",
        direction="nearest",
        tolerance=US_MATCH_TOLERANCE,
    )

    gap_usd = merged["price_usd"] - merged["us_price_usd"]
    gap_pct = gap_usd / merged["us_price_usd"]

    # US rows: gap against themselves is zero when a match exists
    out.loc[merged["index"], "price_gap_usd"] = gap_usd.values
    out.loc[merged["index"], "price_gap_pct"] = gap_pct.values
    return out


def _add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Extract ``hour_of_day`` and ``day_of_week`` from timestamp."""
    out = df.copy()
    if out.empty:
        out["hour_of_day"] = pd.Series(dtype="int64")
        out["day_of_week"] = pd.Series(dtype="object")
        return out

    out["hour_of_day"] = out["timestamp"].dt.hour
    # Monday=0 … Sunday=6, plus a readable label
    out["day_of_week"] = out["timestamp"].dt.day_name()
    return out


def _add_rolling_avg(df: pd.DataFrame) -> pd.DataFrame:
    """
    24-hour rolling average of ``price_usd`` per country.

    Uses a time-based window so irregular collection intervals still work.
    """
    out = df.copy()
    if out.empty:
        out["rolling_avg_price_usd"] = pd.Series(dtype="float64")
        return out

    out = out.sort_values(["country", "timestamp"])
    rolling_values = pd.Series(index=out.index, dtype="float64")
    for _, group in out.groupby("country"):
        indexed = group.set_index("timestamp").sort_index()
        rolled = indexed["price_usd"].rolling("24h", min_periods=1).mean()
        rolling_values.loc[group.index] = rolled.to_numpy()
    out["rolling_avg_price_usd"] = rolling_values
    return out


def _add_arbitrage_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Simple arbitrage score from the US buyer's perspective.

    score = -price_gap_usd - (duty_fraction * price_usd) - shipping_usd

    Interpretation: higher score ⇒ more attractive to buy in that market
    (after rough duty/shipping). US baseline lands near 0.
    """
    out = df.copy()
    if out.empty:
        out["arbitrage_score"] = pd.Series(dtype="float64")
        out["estimated_friction_usd"] = pd.Series(dtype="float64")
        return out

    duty = out["country"].map(DUTY_ESTIMATES).fillna(0.10)
    shipping = out["country"].map(SHIPPING_ESTIMATES_USD).fillna(50.0)
    friction = duty * out["price_usd"] + shipping
    # price_gap_usd is foreign - US; negate so cheaper foreign markets score higher
    out["estimated_friction_usd"] = friction
    out["arbitrage_score"] = (-out["price_gap_usd"].astype("float64")) - friction
    return out


def build_features(db_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Build the full analysis-ready feature table from ``price_snapshots``.

    Columns added:
        price_usd, price_gap_usd, price_gap_pct, hour_of_day, day_of_week,
        rolling_avg_price_usd, estimated_friction_usd, arbitrage_score
    """
    df = load_price_snapshots(db_path)
    if df.empty:
        logger.warning("No price snapshots available — returning empty features")
        return df

    logger.info("Building features for %d snapshot row(s)", len(df))
    df = _add_price_usd(df, db_path=db_path)
    df = _add_us_gaps(df)
    df = _add_time_features(df)
    df = _add_rolling_avg(df)
    df = _add_arbitrage_score(df)
    df = df.sort_values(["timestamp", "country"]).reset_index(drop=True)

    logger.info(
        "Features ready — countries=%s rows=%d",
        sorted(df["country"].unique()),
        len(df),
    )
    return df


def latest_snapshot(features: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Return the most recent row per country from a feature DataFrame.

    Useful for the dashboard's current price-gap table.
    """
    df = features if features is not None else build_features()
    if df.empty:
        return df
    return (
        df.sort_values("timestamp")
        .groupby("country", as_index=False)
        .tail(1)
        .sort_values("price_usd", ascending=True)
        .reset_index(drop=True)
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    feats = build_features()
    if feats.empty:
        print("No data yet — run: python collect.py --once")
    else:
        cols = [
            "country",
            "price",
            "currency",
            "price_usd",
            "price_gap_usd",
            "price_gap_pct",
            "rolling_avg_price_usd",
            "arbitrage_score",
            "hour_of_day",
            "day_of_week",
        ]
        print(feats[cols].to_string(index=False))
        print("\nLatest per country:")
        print(latest_snapshot(feats)[cols].to_string(index=False))
