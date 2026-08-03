"""
Hourly scheduler for cross-market price collection.

Runs ``collect.collect_prices`` once per hour. Use ``--once`` to fire a
single cycle manually (useful for testing without waiting on the schedule).

Usage:
    python scheduler.py --once          # one collection cycle, then exit
    python scheduler.py --once --country US
    python scheduler.py                 # run forever, hourly
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import schedule

from collect import collect_prices
from config import COUNTRY_CODES

logger = logging.getLogger(__name__)


def _next_run_message() -> str:
    """Return a human-readable 'next run at HH:MM' string for the job queue."""
    job = schedule.next_run()
    if job is None:
        return "No upcoming run scheduled."
    return f"Next run at {job.strftime('%H:%M')} ({job.strftime('%Y-%m-%d')})"


def run_collection_cycle(
    countries: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
) -> int:
    """
    Execute one collection cycle and log when the next run is due.

    Returns:
        Number of rows saved by ``collect_prices``.
    """
    targets = countries or COUNTRY_CODES
    logger.info("=== Collection cycle starting (%s) ===", ", ".join(targets))
    saved = collect_prices(countries=targets, db_path=db_path)
    logger.info("=== Collection cycle finished — saved %d row(s) ===", saved)

    # schedule.next_run() is only meaningful when jobs are registered
    if schedule.get_jobs():
        message = _next_run_message()
        print(message)
        logger.info(message)
    else:
        print("Single-run mode — no further runs scheduled.")

    return saved


def start_hourly_scheduler(
    countries: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
    run_immediately: bool = True,
) -> None:
    """
    Schedule collection once per hour and block forever.

    Args:
        countries: Optional country subset (defaults to all configured markets).
        db_path: Optional SQLite path override.
        run_immediately: If True, run one cycle before waiting for the hour mark.
    """
    schedule.every(1).hours.do(
        run_collection_cycle,
        countries=countries,
        db_path=db_path,
    )
    logger.info(
        "Scheduler started at %s — collecting hourly for: %s",
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        ", ".join(countries or COUNTRY_CODES),
    )

    if run_immediately:
        run_collection_cycle(countries=countries, db_path=db_path)
    else:
        print(_next_run_message())
        logger.info(_next_run_message())

    while True:
        schedule.run_pending()
        time.sleep(1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hourly scheduler for iPhone cross-market price collection"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single collection cycle and exit (no hourly loop)",
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
        "--no-immediate",
        action="store_true",
        help="When running hourly, wait for the first hour mark instead of collecting now",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = _parse_args()

    countries = [args.country.upper()] if args.country else None
    db_path = Path(args.db) if args.db else None

    if args.once:
        run_collection_cycle(countries=countries, db_path=db_path)
        return

    start_hourly_scheduler(
        countries=countries,
        db_path=db_path,
        run_immediately=not args.no_immediate,
    )


if __name__ == "__main__":
    main()
