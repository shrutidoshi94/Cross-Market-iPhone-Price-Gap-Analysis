"""
Collect cross-market price snapshots for the tracked iPhone SKU.

Queries PricesAPI.io for each configured country, rotates API keys, and
appends results to the local SQLite ``price_snapshots`` table.

Only **iPhone 17 Pro Max 512GB** matches are kept (other storages rejected).
Outlier prices and missing product page links are skipped.

Usage:
    python collect.py --once --country US   # single-country smoke test
    python collect.py --once                # all countries, one cycle
    python collect.py --reset               # wipe snapshot history, then exit
    python collect.py --reset --once        # wipe, then collect fresh
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# Ask for the max candidates so we can filter down to the right SKU
SEARCH_LIMIT = 5
OFFERS_LIMIT = 5

SEARCH_URL = f"{PRICESAPI_BASE_URL}/products/search"

# Plausible local-currency shelf prices for iPhone 17 Pro Max 512GB.
# Anything outside these bands is treated as a wrong SKU / accessory / error.
PRICE_BOUNDS_LOCAL: Dict[str, Tuple[float, float]] = {
    "USD": (1100.0, 2200.0),
    "GBP": (1000.0, 2000.0),
    "EUR": (1100.0, 2300.0),
    "CAD": (1600.0, 3200.0),
    "AUD": (2000.0, 4000.0),
    "JPY": (200_000.0, 400_000.0),
    "INR": (130_000.0, 250_000.0),
}

_STORAGE_REJECT = re.compile(
    r"\b(?:128|256)\s*[- ]?\s*gb\b|\b(?:1|2)\s*[- ]?\s*tb\b",
    re.IGNORECASE,
)
_STORAGE_512 = re.compile(r"\b512\s*[- ]?\s*gb\b", re.IGNORECASE)
_MODEL_OK = re.compile(r"iphone\s*17\s*pro\s*max", re.IGNORECASE)


def _coerce_price(value: Any) -> Optional[float]:
    """Parse a price that may be int/float/string into float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


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
                title TEXT,
                source_url TEXT
            )
            """
        )
        # Migrate older DBs that pre-date source_url
        cols = {row[1] for row in conn.execute("PRAGMA table_info(price_snapshots)")}
        if "source_url" not in cols:
            conn.execute("ALTER TABLE price_snapshots ADD COLUMN source_url TEXT")
        conn.commit()

    logger.info("SQLite ready at %s", path)
    return path


def reset_price_snapshots(db_path: Optional[Path] = None) -> int:
    """
    Delete all rows from ``price_snapshots`` (keeps FX cache).

    Returns:
        Number of rows deleted.
    """
    path = init_db(db_path)
    with sqlite3.connect(path) as conn:
        cur = conn.execute("DELETE FROM price_snapshots")
        deleted = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()
    logger.warning("Reset price_snapshots — deleted %d row(s) from %s", deleted, path)
    return deleted


def save_snapshot(db_path: Path, row: Dict[str, Any]) -> None:
    """Insert one price snapshot row into SQLite."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO price_snapshots
                (country, retailer, price, currency, rating, timestamp, title, source_url)
            VALUES
                (:country, :retailer, :price, :currency, :rating, :timestamp, :title, :source_url)
            """,
            row,
        )
        conn.commit()


def is_target_sku(title: Optional[str]) -> bool:
    """
    Return True only for iPhone 17 Pro Max **512GB** titles.

    Rejects other storages (128/256GB, 1TB, 2TB) and non–Pro Max models.
    """
    if not title:
        return False
    if not _MODEL_OK.search(title):
        return False
    if _STORAGE_REJECT.search(title):
        return False
    if not _STORAGE_512.search(title):
        return False
    return True


def prefers_cosmic_orange(title: Optional[str]) -> bool:
    """True when the listing mentions Cosmic Orange (preferred color)."""
    return bool(title and re.search(r"cosmic\s*orange", title, re.IGNORECASE))


def is_plausible_price(price: Optional[float], currency: Optional[str]) -> bool:
    """Reject outlier / wrong-SKU prices using per-currency sanity bands."""
    if price is None or currency is None:
        return False
    bounds = PRICE_BOUNDS_LOCAL.get(currency.upper())
    if bounds is None:
        # Unknown currency: allow but log — better than silently dropping
        logger.warning("No price bounds for currency=%s — accepting %.2f", currency, price)
        return price > 0
    low, high = bounds
    return low <= float(price) <= high


def _offer_candidates(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build candidate price rows from a product + its offers.

    Each candidate includes title, price, currency, retailer, rating, source_url.
    """
    title = product.get("title")
    rating = product.get("rating")
    product_price = _coerce_price(product.get("price"))
    product_currency = product.get("currency")
    product_url = product.get("url") or product.get("link") or product.get("product_url")
    base = {
        "title": title,
        "rating": float(rating) if rating is not None else None,
    }

    offers = product.get("offers") or []
    candidates: List[Dict[str, Any]] = []

    for offer in offers:
        url = offer.get("url") or offer.get("seller_url") or product_url
        price = _coerce_price(offer.get("price"))
        if price is None:
            price = product_price
        currency = offer.get("currency") or product_currency
        retailer = offer.get("seller") or product.get("source")
        candidates.append(
            {
                **base,
                "price": price,
                "currency": currency,
                "retailer": retailer,
                "source_url": url,
            }
        )

    # Always include the product-level headline as a fallback candidate
    candidates.append(
        {
            **base,
            "price": product_price,
            "currency": product_currency,
            "retailer": product.get("source"),
            "source_url": product_url
            or (offers[0].get("url") if offers else None)
            or (offers[0].get("seller_url") if offers else None),
        }
    )

    return candidates


def select_best_match(products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Pick the best 512GB Pro Max candidate from an API product list.

    Preference order:
      1. Valid SKU title (must include 512GB)
      2. Plausible local price (outlier filter)
      3. Has a product page URL
      4. Cosmic Orange preferred
      5. Cheapest remaining price
    """
    scored: List[Tuple[Tuple[int, float], Dict[str, Any]]] = []

    for product in products:
        title = product.get("title")
        if not is_target_sku(title):
            logger.info("Reject SKU mismatch: %r", title)
            continue

        for cand in _offer_candidates(product):
            price = cand.get("price")
            currency = cand.get("currency")
            url = cand.get("source_url")

            if price is None:
                logger.info("Reject missing price: %r", title)
                continue
            if not currency:
                logger.info("Reject missing currency: %r", title)
                continue
            if not is_plausible_price(price, currency):
                logger.info(
                    "Reject outlier price: %r @ %s %s (bounds=%s)",
                    title,
                    price,
                    currency,
                    PRICE_BOUNDS_LOCAL.get(str(currency).upper()),
                )
                continue
            if not url:
                logger.info("Reject missing source_url: %r", title)
                continue

            # Sort key: prefer Cosmic Orange (0), then lower price
            color_rank = 0 if prefers_cosmic_orange(title) else 1
            scored.append(((color_rank, float(price)), cand))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0])
    return scored[0][1]


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
    Search PricesAPI.io for ``query`` in one country and return a cleaned row.

    Uses the next rotated API key. On HTTP 429, backs off and retries with
    a fresh key. Missing / invalid matches return None (logged, not raised).
    """
    country_upper = country.upper()
    country_lower = country.lower()
    timestamp = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, MAX_RETRIES + 2):
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
                    "limit": SEARCH_LIMIT,
                    "offers_limit": OFFERS_LIMIT,
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

        best = select_best_match(products)
        if best is None:
            logger.warning(
                "No valid 512GB Pro Max listing for %s after cleaning — skipping",
                country_upper,
            )
            return None

        row = {
            "country": country_upper,
            "retailer": best.get("retailer"),
            "price": float(best["price"]),
            "currency": best.get("currency"),
            "rating": best.get("rating"),
            "timestamp": timestamp,
            "title": best.get("title"),
            "source_url": best.get("source_url"),
        }
        logger.info(
            "Success %s — %s @ %.2f %s (retailer=%s, key #%d, url=%s)",
            country_upper,
            row["title"],
            row["price"],
            row["currency"],
            row["retailer"],
            key_num,
            row["source_url"],
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
        description="Collect iPhone 17 Pro Max 512GB cross-market price snapshots"
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
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe all price_snapshots rows before collecting (or exit if used alone)",
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

    if args.reset:
        reset_price_snapshots(db_path)
        # Bare --reset wipes history and exits; combine with --once to re-collect.
        if not args.once and args.country is None:
            return

    collect_prices(countries=countries, db_path=db_path)


if __name__ == "__main__":
    main()
