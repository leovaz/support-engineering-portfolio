#!/usr/bin/env python3
"""
update_stuck_transactions.py
============================

Support operations script: find transactions that have been stuck in PENDING
for longer than a given threshold (default: 24 hours) and move them to FAILED
so the customer can retry.

Design notes
------------
* The SELECT and the UPDATE run inside a SINGLE transaction, and the SELECT
  takes a row-level lock (FOR UPDATE ... SKIP LOCKED). This prevents a race
  where the payment processor completes a transaction between our read and our
  write, which would otherwise let us overwrite a COMPLETED row with FAILED.
  SKIP LOCKED means that if another process already holds a row, we leave it
  alone instead of blocking the whole batch.
* Every query is parameterized (psycopg2 %(name)s placeholders). No string
  interpolation of user input into SQL.
* A hard batch limit (--limit) acts as a blast-radius guard: if a bug or a bad
  threshold matches 40,000 rows, we only touch the first N and log a warning.
* --dry-run prints exactly what would change without committing, which is what
  you actually want the first time you run this against production.
* Connection is always closed in a finally block, and any failure rolls back.

Usage
-----
    # See what would be affected, change nothing
    python update_stuck_transactions.py --dry-run

    # Apply the change
    python update_stuck_transactions.py

    # Different threshold / bigger batch
    python update_stuck_transactions.py --hours 48 --limit 1000

Configuration is read from environment variables (with the mock values from
the assignment as defaults). Credentials should never be hard-coded:

    PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

Requires: psycopg2-binary  (pip install psycopg2-binary)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover - environment problem, not a runtime bug
    sys.stderr.write(
        "ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary\n"
    )
    sys.exit(3)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SOURCE_STATUS = "PENDING"
TARGET_STATUS = "FAILED"

DEFAULT_STUCK_HOURS = 24
DEFAULT_BATCH_LIMIT = 500

# Exit codes, so this can be wired into cron / a runbook and alert on failure.
EXIT_OK = 0
EXIT_DB_ERROR = 1
EXIT_UNEXPECTED = 2

# NOTE: `created_at` is TIMESTAMP (without time zone), so we compare it against
# NOW() AT TIME ZONE 'UTC' rather than plain NOW(). Comparing a naive column to
# a timestamptz makes PostgreSQL cast using the *session* time zone, which
# silently shifts the cutoff by hours depending on where the script runs.
SELECT_STUCK_SQL = """
    SELECT transaction_id,
           customer_id,
           amount,
           currency,
           status,
           created_at,
           updated_at
      FROM transactions
     WHERE status = %(source_status)s
       AND created_at < (NOW() AT TIME ZONE 'UTC')
                        - (%(hours)s * INTERVAL '1 hour')
     ORDER BY created_at ASC
     LIMIT %(limit)s
       FOR UPDATE SKIP LOCKED
"""

UPDATE_STUCK_SQL = """
    UPDATE transactions
       SET status     = %(target_status)s,
           updated_at = (NOW() AT TIME ZONE 'UTC')
     WHERE transaction_id = ANY(%(transaction_ids)s)
       AND status = %(source_status)s
 RETURNING transaction_id,
           customer_id,
           amount,
           currency,
           status,
           updated_at
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def build_logger(verbose: bool) -> logging.Logger:
    """Log to stdout with timestamps so output is greppable in a ticket."""
    logger = logging.getLogger("stuck_tx")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    )
    logger.addHandler(handler)
    return logger


def db_config() -> Dict[str, Any]:
    """Read connection details from the environment, with mock defaults."""
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "ops_support"),
        "user": os.getenv("PGUSER", "support_user"),
        "password": os.getenv("PGPASSWORD", "support_pass"),
        # Fail fast instead of hanging forever if the host is unreachable.
        "connect_timeout": 10,
        # Shows up in pg_stat_activity, so a DBA can see who ran this.
        "application_name": "support/update_stuck_transactions",
    }


def format_row(row: Dict[str, Any]) -> str:
    """One-line, aligned summary of a transaction for the log."""
    amount = row["amount"]
    if isinstance(amount, Decimal):
        amount = f"{amount:.2f}"
    return (
        f"{row['transaction_id']:<14} "
        f"customer={row['customer_id']:<12} "
        f"amount={amount:>10} {row['currency']:<4} "
        f"created_at={row['created_at']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move transactions stuck in PENDING past a time threshold to FAILED."
        )
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_STUCK_HOURS,
        help=f"Age threshold in hours (default: {DEFAULT_STUCK_HOURS}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_LIMIT,
        help=f"Max rows to touch in one run (default: {DEFAULT_BATCH_LIMIT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated, then roll back without committing.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    # Validate before we open a connection.
    if args.hours <= 0:
        parser.error("--hours must be a positive integer.")
    if args.limit <= 0:
        parser.error("--limit must be a positive integer.")
    return args


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #

def process_stuck_transactions(
    conn, logger: logging.Logger, hours: int, limit: int, dry_run: bool
) -> int:
    """
    Find and update stuck transactions inside one transaction block.

    Returns the number of rows updated (0 if none matched, or if dry-run).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:

        # ---- 1. Find candidates and lock them -------------------------------
        logger.info(
            "Searching for transactions with status=%s older than %d hour(s)...",
            SOURCE_STATUS,
            hours,
        )
        cur.execute(
            SELECT_STUCK_SQL,
            {
                "source_status": SOURCE_STATUS,
                "hours": hours,
                "limit": limit,
            },
        )
        candidates: List[Dict[str, Any]] = cur.fetchall()

        if not candidates:
            logger.info("No stuck transactions found. Nothing to do.")
            return 0

        logger.info("Found %d stuck transaction(s):", len(candidates))
        for row in candidates:
            logger.info("  -> %s", format_row(row))

        if len(candidates) == limit:
            logger.warning(
                "Batch limit of %d reached - there may be more stuck rows. "
                "Re-run the script or raise --limit after reviewing.",
                limit,
            )

        # ---- 2. Dry-run exit -------------------------------------------------
        if dry_run:
            logger.warning(
                "DRY RUN: %d transaction(s) would be set to %s. "
                "Rolling back, no changes committed.",
                len(candidates),
                TARGET_STATUS,
            )
            conn.rollback()
            return 0

        # ---- 3. Apply the update --------------------------------------------
        transaction_ids = [row["transaction_id"] for row in candidates]
        cur.execute(
            UPDATE_STUCK_SQL,
            {
                "target_status": TARGET_STATUS,
                "source_status": SOURCE_STATUS,
                "transaction_ids": transaction_ids,
            },
        )
        updated = cur.fetchall()

        # RETURNING gives us the post-update state straight from the database,
        # so the audit log reflects what was actually written, not what we hoped.
        logger.info("Updated %d transaction(s) to %s:", len(updated), TARGET_STATUS)
        for row in updated:
            logger.info(
                "  -> %s  %s -> %s  (updated_at=%s)",
                row["transaction_id"],
                SOURCE_STATUS,
                row["status"],
                row["updated_at"],
            )

        # Defensive check: the lock should make this impossible, but if the
        # counts disagree, something changed the rows underneath us.
        if len(updated) != len(candidates):
            logger.warning(
                "Selected %d row(s) but updated %d. "
                "Some rows changed status concurrently and were skipped.",
                len(candidates),
                len(updated),
            )

        conn.commit()
        logger.info("Transaction committed.")
        return len(updated)


def main() -> int:
    args = parse_args()
    logger = build_logger(args.verbose)

    config = db_config()
    logger.info(
        "Connecting to postgresql://%s@%s:%s/%s",
        config["user"],
        config["host"],
        config["port"],
        config["dbname"],
    )

    conn = None
    try:
        conn = psycopg2.connect(**config)
        # Explicit control: we commit or roll back ourselves, never implicitly.
        conn.autocommit = False

        updated_count = process_stuck_transactions(
            conn,
            logger,
            hours=args.hours,
            limit=args.limit,
            dry_run=args.dry_run,
        )

        logger.info("Done. %d transaction(s) updated.", updated_count)
        return EXIT_OK

    except psycopg2.OperationalError as exc:
        # Wrong credentials, host down, database missing, timeout.
        logger.error("Could not connect to the database: %s", str(exc).strip())
        return EXIT_DB_ERROR

    except psycopg2.DatabaseError as exc:
        # Constraint violation, bad SQL, permission denied, deadlock.
        logger.error("Database error, rolling back: %s", str(exc).strip())
        if conn is not None:
            conn.rollback()
        return EXIT_DB_ERROR

    except KeyboardInterrupt:
        logger.warning("Interrupted by user, rolling back.")
        if conn is not None:
            conn.rollback()
        return EXIT_UNEXPECTED

    except Exception as exc:  # noqa: BLE001 - last line of defence for a cron job
        logger.exception("Unexpected error, rolling back: %s", exc)
        if conn is not None:
            conn.rollback()
        return EXIT_UNEXPECTED

    finally:
        # Runs on every path, including the successful one.
        if conn is not None and not conn.closed:
            conn.close()
            logger.debug("Database connection closed.")


if __name__ == "__main__":
    sys.exit(main())
