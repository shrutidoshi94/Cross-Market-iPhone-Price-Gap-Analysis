"""
Configuration and API key rotation for PricesAPI.io.

Loads up to 5 API keys from environment variables and exposes a
round-robin KeyRotator that distributes requests across accounts while
respecting a per-key rate limit.
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Deque, Iterator, List, Optional, Tuple

from dotenv import load_dotenv

# Always load .env from this project directory (not the caller's cwd)
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _load_streamlit_secrets_into_env() -> None:
    """
    On Streamlit Community Cloud, secrets live in st.secrets — not .env.

    Copy known keys into os.environ so the rest of the app can keep using
    os.getenv(...) locally, in Actions, and on Streamlit Cloud.
    """
    try:
        import streamlit as st  # optional dependency at import time
    except ImportError:
        return

    try:
        secrets = st.secrets  # type: ignore[attr-defined]
    except Exception:
        return

    keys = [f"PRICESAPI_KEY_{i}" for i in range(1, 6)]
    keys.append("EXCHANGERATE_HOST_ACCESS_KEY")
    keys.append("DATABASE_PATH")

    for key in keys:
        if key in os.environ and os.environ[key].strip():
            continue
        try:
            value = secrets.get(key)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value:
            os.environ[key] = str(value)


_load_streamlit_secrets_into_env()

logger = logging.getLogger(__name__)

# Product we track across markets
PRODUCT_QUERY = "iPhone 17 Pro Max 512GB Cosmic Orange"

# Markets to collect (ISO 3166-1 alpha-2)
COUNTRY_CODES = ["US", "GB", "DE", "FR", "CA", "AU", "JP", "IN"]

# PricesAPI.io base URL
PRICESAPI_BASE_URL = "https://api.pricesapi.io/api/v1"

# Default SQLite path (used by later modules)
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/prices.db")

# Rate limit: 10 requests/minute per key (project requirement)
REQUESTS_PER_MINUTE_PER_KEY = 10

# How long to sleep when the API returns HTTP 429
RATE_LIMIT_BACKOFF_SECONDS = 60


def load_api_keys() -> List[str]:
    """
    Load PricesAPI.io keys from .env into a list.

    Reads PRICESAPI_KEY_1 through PRICESAPI_KEY_5. Empty or missing
    placeholders are skipped so a single-key setup still works for testing.

    Returns:
        Non-empty list of API key strings.

    Raises:
        ValueError: If no usable keys are found.
    """
    keys: List[str] = []
    for i in range(1, 6):
        raw = os.getenv(f"PRICESAPI_KEY_{i}", "").strip()
        # Skip blanks and the .env.example placeholder text
        if not raw or raw == "your_pricesapi_key_here":
            continue
        keys.append(raw)

    if not keys:
        raise ValueError(
            "No PricesAPI.io keys found. Copy .env.example to .env and set "
            "at least PRICESAPI_KEY_1."
        )

    logger.info("Loaded %d PricesAPI.io key(s)", len(keys))
    return keys


class KeyRotator:
    """
    Round-robin API key rotator with a simple per-key rate limiter.

    Each call to ``next_key()`` returns the next key in sequence and
    blocks briefly if that key has already hit REQUESTS_PER_MINUTE_PER_KEY
    within the last 60 seconds. On HTTP 429, call ``backoff()`` to sleep
    and log which key was limited.
    """

    def __init__(
        self,
        keys: Optional[List[str]] = None,
        requests_per_minute: int = REQUESTS_PER_MINUTE_PER_KEY,
        backoff_seconds: float = RATE_LIMIT_BACKOFF_SECONDS,
    ) -> None:
        self.keys = keys if keys is not None else load_api_keys()
        if not self.keys:
            raise ValueError("KeyRotator requires at least one API key")

        self.requests_per_minute = requests_per_minute
        self.backoff_seconds = backoff_seconds

        # Round-robin cycle over (1-based label, key) pairs for clear logs
        self._cycle: Iterator[Tuple[int, str]] = itertools.cycle(
            enumerate(self.keys, start=1)
        )

        # Per-key timestamps of recent requests (sliding 60s window)
        self._request_times: dict[int, Deque[float]] = {
            i: deque() for i in range(1, len(self.keys) + 1)
        }

    def __iter__(self) -> "KeyRotator":
        return self

    def __next__(self) -> Tuple[int, str]:
        """Allow ``for key_num, key in rotator:`` style usage."""
        return self.next_key()

    def next_key(self) -> Tuple[int, str]:
        """
        Return the next ``(key_number, api_key)`` pair, waiting if needed
        so this key stays under the per-minute request budget.

        Returns:
            Tuple of (1-based key index for logging, API key string).
        """
        key_num, key = next(self._cycle)
        self._wait_for_capacity(key_num)
        self._request_times[key_num].append(time.monotonic())
        logger.debug("Using API key #%d", key_num)
        return key_num, key

    def backoff(self, key_num: int, retry_after: Optional[float] = None) -> None:
        """
        Sleep after a 429 response and log which key hit the limit.

        Args:
            key_num: 1-based key index that received the 429.
            retry_after: Optional seconds from a Retry-After header; falls
                back to RATE_LIMIT_BACKOFF_SECONDS when not provided.
        """
        wait = retry_after if retry_after is not None else self.backoff_seconds
        logger.warning(
            "Rate limit hit on API key #%d — backing off for %.1f seconds",
            key_num,
            wait,
        )
        time.sleep(wait)

    def _wait_for_capacity(self, key_num: int) -> None:
        """Block until this key has a free slot in its 60-second window."""
        window = 60.0
        times = self._request_times[key_num]
        now = time.monotonic()

        # Drop timestamps older than 60 seconds
        while times and now - times[0] >= window:
            times.popleft()

        if len(times) < self.requests_per_minute:
            return

        # Oldest request in the window must age out before we can fire again
        sleep_for = window - (now - times[0]) + 0.05  # tiny buffer
        if sleep_for > 0:
            logger.info(
                "Key #%d at %d req/min cap — sleeping %.2fs",
                key_num,
                self.requests_per_minute,
                sleep_for,
            )
            time.sleep(sleep_for)

            # Clean the window again after sleeping
            now = time.monotonic()
            while times and now - times[0] >= window:
                times.popleft()


def get_rotator() -> KeyRotator:
    """Convenience factory: load keys from .env and return a KeyRotator."""
    return KeyRotator()


if __name__ == "__main__":
    # Quick smoke test: print how many keys loaded (never print the keys)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        rotator = get_rotator()
        print(f"OK — loaded {len(rotator.keys)} key(s)")
        for _ in range(min(3, len(rotator.keys))):
            num, _key = rotator.next_key()
            print(f"  next → key #{num}")
    except ValueError as exc:
        print(f"Config error: {exc}")
