"""
Collect cross-market price snapshots for the tracked iPhone SKU.

Queries PricesAPI.io for each configured country, rotates API keys, and
appends results to the local SQLite ``price_snapshots`` table.

Usage:
    python collect.py --once --country US   # single-country smoke test
    python collect.py --once                # all countries, one cycle
    python collect.py                       # same as --once (collection script)
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import (
    COUNTRY_CODES,
    DATABASE_PATH,
    PRODUCT_QUERY,
    PRICESAPI_BASE_URL,
    KeyRotator,
    get_rotator,
)

logger = logging.getLogger(__name__)

# Cold PricesAPI searches can take 30–90s; docs recommend ≥ 95s timeout
REQUEST_TIMEOUT_SECONDS = 95

# Max retries for a single country after 429 / transient errors
MAX_RETRIES = 2

SEARCH_URL = f"{PRICESAPI_BASE_URL}/products/search"


def _db_path() -> Path:
    """Resolve DATABASE_PATH relative to this project when it is not absolute."""
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def init_db(db_path: Optional[Path] = None) -> Path:
    """
    Create the SQLite file and ``price_snapshots`` table if needed.

    Returns:
        Path to the database file.
    """
    path = db_path or _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country TEXT NOT NULL,
                retailer TEXT,
                price REAL,
                currency TEXT,
                rating REAL,
                timestamp TEXT NOT NULL,
                title TEXT
            )
            """
        )
        conn.commit()

    logger.info("SQLite ready at %s", path)
    return path


def save_snapshot(db_path: Path, row: Dict[str, Any]) -> None:
    """Insert one price snapshot row into SQLite."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO price_snapshots
                (country, retailer, price, currency, rating, timestamp, title)
            VALUES
                (:country, :retailer, :price, :currency, :rating, :timestamp, :title)
            """,
            row,
        )
        conn.commit()


def _log_quota_hints(response: requests.Response, payload: Dict[str, Any]) -> None:
    """Log token/quota-related headers or meta fields when the API provides them."""
    interesting_headers = {
        k: v
        for k, v in response.headers.items()
        if any(
            token in k.lower()
            for token in ("rate", "limit", "remaining", "credit", "quota")
        )
    }
    if interesting_headers:
        logger.info("Quota/rate headers: %s", interesting_headers)

    meta = payload.get("meta") or {}
    if meta:
        # Useful diagnostics even when no explicit remaining-credit field exists
        logger.info(
            "API meta — cache=%s latency_ms=%s degraded=%s",
            meta.get("cache_source"),
            meta.get("latency_ms"),
            meta.get("degraded"),
        )


def fetch_product_for_country(
    country: str,
    rotator: KeyRotator,
    query: str = PRODUCT_QUERY,
) -> Optional[Dict[str, Any]]:
    """
    Search PricesAPI.io for ``query`` in one country and return a parsed row.

    Uses the next rotated API key. On HTTP 429, backs off and retries with
    a fresh key. Missing matches return None (logged, not raised).

    Args:
        country: ISO country code (e.g. ``US``). API expects lowercase.
        rotator: Shared KeyRotator instance.
        query: Product search string.

    Returns:
        Dict ready for ``save_snapshot``, or None if no usable match.
    """
    country_upper = country.upper()
    country_lower = country.lower()
    timestamp = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, MAX_RETRIES + 2):  # initial try + retries
        key_num, api_key = rotator.next_key()
        logger.info(
            "Querying country=%s with key #%d (attempt %d)",
            country_upper,
            key_num,
            attempt,
        )

        try:
            response = requests.get(
                SEARCH_URL,
                params={
                    "q": query,
                    "country": country_lower,
                    "limit": 3,
                    "offers_limit": 3,
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.error(
                "Request failed for %s (key #%d): %s",
                country_upper,
                key_num,
                exc,
            )
            if attempt <= MAX_RETRIES:
                continue
            return None

        if response.status_code == 429:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after = float(retry_after_raw) if retry_after_raw else None
            rotator.backoff(key_num, retry_after=retry_after)
            continue

        if response.status_code >= 400:
            logger.error(
                "API error for %s (key #%d): HTTP %s — %s",
                country_upper,
                key_num,
                response.status_code,
                response.text[:300],
            )
            # Credits exceeded / auth issues — don't burn retries on same shape
            if response.status_code in (401, 403):
                return None
            if attempt <= MAX_RETRIES:
                continue
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.error("Non-JSON response for %s (key #%d)", country_upper, key_num)
            return None

        _log_quota_hints(response, payload)

        products = (payload.get("data") or {}).get("products") or []
        if not products:
            logger.warning(
                "No product match for country=%s (key #%d) — skipping",
                country_upper,
                key_num,
            )
            return None

        # Prefer the top candidate; fall back to cheapest offer if present
        product = products[0]
        price = product.get("price")
        currency = product.get("currency")
        retailer = product.get("source")
        rating = product.get("rating")
        title = product.get("title")

        offers = product.get("offers") or []
        if offers:
            # Offers are cheapest-first per API docs
            best = offers[0]
            if best.get("price") is not None:
                price = best.get("price")
            if best.get("currency"):
                currency = best.get("currency")
            if best.get("seller"):
                retailer = best.get("seller")

        if price is None:
            logger.warning(
                "Match for %s has no price (title=%r) — skipping",
                country_upper,
                title,
            )
            return None

        row = {
            "country": country_upper,
            "retailer": retailer,
            "price": float(price),
            "currency": currency,
            "rating": float(rating) if rating is not None else None,
            "timestamp": timestamp,
            "title": title,
        }
        logger.info(
            "Success %s — %s @ %.2f %s (retailer=%s, key #%d)",
            country_upper,
            title,
            row["price"],
            currency,
            retailer,
            key_num,
        )
        return row

    logger.error("Exhausted retries for country=%s", country_upper)
    return None


def collect_prices(
    countries: Optional[List[str]] = None,
    rotator: Optional[KeyRotator] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    Run one full collection cycle across ``countries``.

    Args:
        countries: Country codes to query. Defaults to ``COUNTRY_CODES``.
        rotator: Optional shared rotator (created if omitted).
        db_path: Optional SQLite path override.

    Returns:
        Number of rows successfully written.
    """
    targets = [c.upper() for c in (countries or COUNTRY_CODES)]
    rotator = rotator or get_rotator()
    path = init_db(db_path)

    logger.info(
        "Starting collection for %d country(ies): %s",
        len(targets),
        ", ".join(targets),
    )
    saved = 0
    for country in targets:
        row = fetch_product_for_country(country, rotator)
        if row is None:
            continue
        save_snapshot(path, row)
        saved += 1

    logger.info("Collection complete — saved %d / %d countries", saved, len(targets))
    return saved


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect iPhone cross-market price snapshots from PricesAPI.io"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single collection cycle and exit (default behavior for this script)",
    )
    parser.add_argument(
        "--country",
        type=str,
        default=None,
        help="Optional single country code for testing (e.g. US)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional SQLite path override",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = _parse_args()

    countries = [args.country] if args.country else None
    db_path = Path(args.db) if args.db else None

    # Always a single cycle here; --once is accepted for symmetry with scheduler.py
    collect_prices(countries=countries, db_path=db_path)


if __name__ == "__main__":
    main()
