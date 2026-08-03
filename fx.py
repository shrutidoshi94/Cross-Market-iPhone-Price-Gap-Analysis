"""
FX rate fetching and USD normalization via exchangerate.host.

Fetches daily currency→USD rates, caches them in SQLite (``fx_rates``), and
exposes ``normalize_to_usd()`` for converting local prices.

Requires ``EXCHANGERATE_HOST_ACCESS_KEY`` in ``.env``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import requests

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

# Currencies we expect across the tracked markets
DEFAULT_CURRENCIES = ("USD", "GBP", "EUR", "CAD", "AUD", "JPY", "INR")

EXCHANGERATE_HOST_URL = "https://api.exchangerate.host/historical"
REQUEST_TIMEOUT_SECONDS = 30


def _db_path(db_path: Optional[Path] = None) -> Path:
    """Resolve the SQLite path relative to the project root when needed."""
    if db_path is not None:
        return Path(db_path)
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def init_fx_table(db_path: Optional[Path] = None) -> Path:
    """
    Ensure the ``fx_rates`` table exists.

    Schema: ``date, currency, rate_to_usd`` where ``rate_to_usd`` means
    "1 unit of ``currency`` equals this many USD".
    """
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fx_rates (
                date TEXT NOT NULL,
                currency TEXT NOT NULL,
                rate_to_usd REAL NOT NULL,
                PRIMARY KEY (date, currency)
            )
            """
        )
        conn.commit()

    return path


def _parse_date(value: Union[str, date, datetime]) -> date:
    """Normalize various date-like inputs to a ``datetime.date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # Accept ISO timestamps like 2026-08-03T21:00:00+00:00
    return date.fromisoformat(str(value)[:10])


def get_cached_rate(
    currency: str,
    on_date: Union[str, date, datetime],
    db_path: Optional[Path] = None,
) -> Optional[float]:
    """Return a cached ``rate_to_usd`` for an exact date, or None."""
    path = init_fx_table(db_path)
    day = _parse_date(on_date).isoformat()
    currency = currency.upper()

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT rate_to_usd FROM fx_rates
            WHERE date = ? AND currency = ?
            """,
            (day, currency),
        ).fetchone()

    return float(row[0]) if row else None


def get_closest_rate(
    currency: str,
    on_date: Union[str, date, datetime],
    db_path: Optional[Path] = None,
) -> Optional[tuple[str, float]]:
    """
    Look up the closest cached rate on or before ``on_date``, else after.

    Returns:
        ``(rate_date, rate_to_usd)`` or None if nothing is cached.
    """
    path = init_fx_table(db_path)
    day = _parse_date(on_date).isoformat()
    currency = currency.upper()

    with sqlite3.connect(path) as conn:
        before = conn.execute(
            """
            SELECT date, rate_to_usd FROM fx_rates
            WHERE currency = ? AND date <= ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (currency, day),
        ).fetchone()
        after = conn.execute(
            """
            SELECT date, rate_to_usd FROM fx_rates
            WHERE currency = ? AND date >= ?
            ORDER BY date ASC
            LIMIT 1
            """,
            (currency, day),
        ).fetchone()

    if before and after:
        target = _parse_date(on_date)
        d_before = abs((target - date.fromisoformat(before[0])).days)
        d_after = abs((date.fromisoformat(after[0]) - target).days)
        chosen = before if d_before <= d_after else after
        return chosen[0], float(chosen[1])
    if before:
        return before[0], float(before[1])
    if after:
        return after[0], float(after[1])
    return None


def _store_rates(
    on_date: date,
    rates_to_usd: dict[str, float],
    db_path: Optional[Path] = None,
) -> None:
    """Upsert a batch of rates for one calendar date."""
    path = init_fx_table(db_path)
    day = on_date.isoformat()
    rows = [(day, cur.upper(), float(rate)) for cur, rate in rates_to_usd.items()]

    with sqlite3.connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO fx_rates (date, currency, rate_to_usd)
            VALUES (?, ?, ?)
            ON CONFLICT(date, currency) DO UPDATE SET
                rate_to_usd = excluded.rate_to_usd
            """,
            rows,
        )
        conn.commit()

    logger.info("Cached %d FX rate(s) for %s", len(rows), day)


def _rates_from_usd_quotes(quotes: dict[str, float]) -> dict[str, float]:
    """
    Convert exchangerate.host ``quotes`` (USDxxx → units per 1 USD)
    into ``rate_to_usd`` (1 unit of currency → USD).
    """
    out = {"USD": 1.0}
    for pair, units_per_usd in quotes.items():
        pair = pair.upper()
        if len(pair) == 6 and pair.startswith("USD"):
            currency = pair[3:]
        else:
            # Plain currency code fallback (older response shapes)
            currency = pair

        if currency == "USD":
            out["USD"] = 1.0
            continue
        if not units_per_usd:
            logger.warning("Skipping zero/missing rate for %s", currency)
            continue
        out[currency] = 1.0 / float(units_per_usd)
    return out


def _get_access_key() -> str:
    """Return the exchangerate.host access key or raise a clear error."""
    access_key = os.getenv("EXCHANGERATE_HOST_ACCESS_KEY", "").strip()
    if not access_key:
        raise RuntimeError(
            "EXCHANGERATE_HOST_ACCESS_KEY is not set. "
            "Add it to your .env file (see .env.example)."
        )
    return access_key


def _fetch_exchangerate_host(
    on_date: date,
    currencies: Sequence[str],
) -> dict[str, float]:
    """
    Fetch rates from exchangerate.host for ``on_date``.

    Raises:
        RuntimeError: If the key is missing or the API call fails.
    """
    access_key = _get_access_key()
    symbols = ",".join(c for c in currencies if c.upper() != "USD")
    params = {
        "access_key": access_key,
        "date": on_date.isoformat(),
        # APILayer-hosted exchangerate.host uses source= for the base currency
        "source": "USD",
        "currencies": symbols,
    }

    try:
        response = requests.get(
            EXCHANGERATE_HOST_URL,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"exchangerate.host request failed: {exc}") from exc

    if not payload.get("success", False):
        raise RuntimeError(f"exchangerate.host error: {payload.get('error')}")

    quotes = payload.get("quotes") or {}
    if not quotes and payload.get("rates"):
        # Normalize a plain rates map into USDXXX quote keys
        quotes = {
            f"USD{code.upper()}": value
            for code, value in payload["rates"].items()
        }

    if not quotes:
        raise RuntimeError(f"exchangerate.host returned no quotes: {payload}")

    rates = _rates_from_usd_quotes(quotes)
    # Keep only currencies we care about (+ USD)
    wanted = {c.upper() for c in currencies} | {"USD"}
    return {cur: rate for cur, rate in rates.items() if cur in wanted}


def fetch_and_cache_rates(
    on_date: Union[str, date, datetime, None] = None,
    currencies: Optional[Iterable[str]] = None,
    db_path: Optional[Path] = None,
    force: bool = False,
) -> dict[str, float]:
    """
    Fetch FX rates for ``on_date`` and cache them locally.

    Skips the network call when every requested currency is already cached,
    unless ``force=True``.

    Returns:
        Mapping of currency code → rate_to_usd for the requested date.
    """
    day = _parse_date(on_date or date.today())
    wanted = [c.upper() for c in (currencies or DEFAULT_CURRENCIES)]

    if not force:
        missing = [c for c in wanted if get_cached_rate(c, day, db_path) is None]
        if not missing:
            logger.info("FX cache hit for %s (%s)", day, ", ".join(wanted))
            return {c: float(get_cached_rate(c, day, db_path)) for c in wanted}  # type: ignore[arg-type]
    else:
        missing = wanted

    logger.info("Fetching FX rates for %s (need: %s)", day, ", ".join(missing))
    rates = _fetch_exchangerate_host(day, wanted)
    rates["USD"] = 1.0
    _store_rates(day, rates, db_path)
    logger.info("FX rates fetched via exchangerate.host for %s", day)

    result: dict[str, float] = {}
    for currency in wanted:
        if currency in rates:
            result[currency] = rates[currency]
        else:
            cached = get_cached_rate(currency, day, db_path)
            if cached is None:
                logger.warning("No rate returned for %s on %s", currency, day)
            else:
                result[currency] = cached
    return result


def normalize_to_usd(
    price: float,
    currency: str,
    on_date: Union[str, date, datetime],
    db_path: Optional[Path] = None,
) -> Optional[float]:
    """
    Convert ``price`` in ``currency`` to USD using the closest available FX rate.

    Looks in the local cache first, fetches the requested date if missing, then
    falls back to the nearest cached day.

    Returns:
        USD amount, or None if no rate can be resolved.
    """
    if price is None:
        return None

    currency = (currency or "USD").upper()
    if currency == "USD":
        return float(price)

    day = _parse_date(on_date)

    rate = get_cached_rate(currency, day, db_path)
    if rate is None:
        try:
            fetch_and_cache_rates(day, currencies=DEFAULT_CURRENCIES, db_path=db_path)
            rate = get_cached_rate(currency, day, db_path)
        except RuntimeError as exc:
            logger.warning("FX fetch failed during normalize: %s", exc)

    if rate is None:
        closest = get_closest_rate(currency, day, db_path)
        if closest is None:
            logger.error("No FX rate available for %s near %s", currency, day)
            return None
        rate_date, rate = closest
        logger.info(
            "Using closest FX rate for %s: %s (requested %s)",
            currency,
            rate_date,
            day,
        )

    return float(price) * float(rate)


def ensure_rates_for_dates(
    dates: Sequence[Union[str, date, datetime]],
    currencies: Optional[Iterable[str]] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Fetch/cache rates for each unique calendar date in ``dates``."""
    unique_days = sorted({_parse_date(d) for d in dates})
    for day in unique_days:
        fetch_and_cache_rates(day, currencies=currencies, db_path=db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    today = date.today()
    rates = fetch_and_cache_rates(today, force=True)
    print(f"Rates for {today}:")
    for cur in sorted(rates):
        print(f"  1 {cur} = {rates[cur]:.6f} USD")

    print("normalize 100 USD →", normalize_to_usd(100, "USD", today))
    print("normalize 100 GBP →", normalize_to_usd(100, "GBP", today))
    print("normalize 100 EUR →", normalize_to_usd(100, "EUR", today))
