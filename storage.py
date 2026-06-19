"""
storage.py
==========
Phase 1b — the persistence layer.

Why does this project need a database at all? Two reasons that matter for a
credible analyst-facing tool:

  1. VALIDATION. A risk score nobody can check is just an opinion. To later show
     "our score moved BEFORE freight rates jumped", we need a stored history of
     what the score (and its inputs) were on each past day. The market APIs give
     us 90 days of FX/oil/freight history we can backfill, but the NEWS sentiment
     only exists "today" — so unless we WRITE IT DOWN each day, it's gone
     forever. This store captures it.

  2. TRENDS. Phase 3's dashboard charts and the daily briefing read straight out
     of this table instead of re-hitting every API on every page load.

We use SQLite: a single self-contained file (`trade_lane.db`), zero server to
run, built into Python's standard library. Perfect for a portfolio project that
has to "just work" when someone clones it.

The schema is deliberately one wide row per (date, lane):

    date | lane | fx_rate | oil_price | news_sentiment | risk_score | components(JSON)

`components` holds the per-input sub-scores as JSON so we can chart the
breakdown later without adding a column every time the model changes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

# The database lives next to the code so the path works no matter where the
# project is run from.
DB_PATH = Path(__file__).with_name("trade_lane.db")

# One CREATE statement, run every time we connect. "IF NOT EXISTS" makes it
# idempotent — safe to call repeatedly, does nothing once the table is there.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date            TEXT NOT NULL,   -- ISO date 'YYYY-MM-DD'
    lane            TEXT NOT NULL,   -- e.g. 'US', 'AU'
    fx_rate         REAL,            -- destination currency per 1 EUR, that day
    oil_price       REAL,            -- Brent USD/bbl, that day
    news_sentiment  REAL,            -- -1..+1, snapshot-only (why we persist it)
    risk_score      REAL,            -- 0..100 final score (filled by Phase 2)
    band            TEXT,            -- risk band label that day (drives alerts)
    landed_cost     REAL,            -- total landed cost EUR that day (drives alerts)
    components      TEXT,            -- JSON: per-input sub-scores/details
    updated_at      TEXT NOT NULL,   -- when this row was last written
    PRIMARY KEY (date, lane)         -- one row per lane per day; re-runs UPSERT
);
"""


@contextmanager
def get_connection(db_path: Path | str = DB_PATH):
    """
    Open a SQLite connection as a context manager so callers can write:

        with get_connection() as conn:
            save_snapshot(conn, ...)

    The `with` block guarantees the connection is committed and closed even if
    an error happens mid-write. `row_factory = sqlite3.Row` lets us read columns
    by name (row["risk_score"]) instead of by numeric index — much clearer.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)  # ensure the table exists before anyone uses it
    _migrate(conn)         # add any columns missing from an older DB
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the original schema shipped. CREATE TABLE IF NOT EXISTS
# won't add these to a pre-existing DB, so we ALTER them in idempotently.
_LATER_COLUMNS = {"band": "TEXT", "landed_cost": "REAL"}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(snapshots)")}
    for col, coltype in _LATER_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {coltype}")


def save_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str | None = None,
    lane: str,
    fx_rate: float | None = None,
    oil_price: float | None = None,
    news_sentiment: float | None = None,
    risk_score: float | None = None,
    band: str | None = None,
    landed_cost: float | None = None,
    components: dict | None = None,
) -> None:
    """
    Insert or update one lane's snapshot for a given day.

    We use SQLite's "UPSERT" (INSERT ... ON CONFLICT ... DO UPDATE). Because the
    primary key is (date, lane), running the monitor twice on the same day
    OVERWRITES that day's row instead of creating a duplicate — so you always
    have exactly one authoritative row per lane per day.

    All metric args are keyword-only and optional, so Phase 1b can write just
    the market inputs now, and Phase 2 can come back and fill in sentiment +
    risk_score for the same row.
    """
    snapshot_date = snapshot_date or date.today().isoformat()
    components_json = json.dumps(components) if components is not None else None

    conn.execute(
        """
        INSERT INTO snapshots
            (date, lane, fx_rate, oil_price,
             news_sentiment, risk_score, band, landed_cost, components, updated_at)
        VALUES
            (:date, :lane, :fx_rate, :oil_price,
             :news_sentiment, :risk_score, :band, :landed_cost, :components, datetime('now'))
        ON CONFLICT(date, lane) DO UPDATE SET
            -- COALESCE(new, old): only overwrite a column when the new value is
            -- non-NULL, so a partial later write never wipes earlier data.
            fx_rate        = COALESCE(excluded.fx_rate, snapshots.fx_rate),
            oil_price      = COALESCE(excluded.oil_price, snapshots.oil_price),
            news_sentiment = COALESCE(excluded.news_sentiment, snapshots.news_sentiment),
            risk_score     = COALESCE(excluded.risk_score, snapshots.risk_score),
            band           = COALESCE(excluded.band, snapshots.band),
            landed_cost    = COALESCE(excluded.landed_cost, snapshots.landed_cost),
            components     = COALESCE(excluded.components, snapshots.components),
            updated_at     = datetime('now')
        """,
        {
            "date": snapshot_date,
            "lane": lane,
            "fx_rate": fx_rate,
            "oil_price": oil_price,
            "news_sentiment": news_sentiment,
            "risk_score": risk_score,
            "band": band,
            "landed_cost": landed_cost,
            "components": components_json,
        },
    )


def load_history(conn: sqlite3.Connection, lane: str, limit: int = 90) -> list[dict]:
    """
    Return the most recent `limit` snapshots for one lane, oldest-first (so it's
    ready to plot left-to-right). Each row comes back as a plain dict with
    `components` already decoded from JSON.
    """
    rows = conn.execute(
        """
        SELECT * FROM snapshots
        WHERE lane = :lane
        ORDER BY date DESC
        LIMIT :limit
        """,
        {"lane": lane, "limit": limit},
    ).fetchall()

    history = []
    for row in reversed(rows):  # flip DESC -> chronological
        item = dict(row)
        if item.get("components"):
            item["components"] = json.loads(item["components"])
        history.append(item)
    return history


def backfill_market_history(conn: sqlite3.Connection, bundle: dict) -> int:
    """
    Seed the database with the historical market series the APIs already give us
    (FX, oil, freight all carry ~90 days of past values). This means the trend
    charts and validation have real history on day ONE, instead of waiting weeks
    for snapshots to pile up.

    `bundle` is the dict returned by data_sources.fetch_all(). We DON'T backfill
    news sentiment or risk_score here — those only exist from the day the
    monitor first runs forward, and Phase 2 writes them.

    Returns the number of (date, lane) rows touched.
    """
    from data_sources import LANES  # local import avoids a circular dependency

    fx = bundle.get("fx", {})
    oil = bundle.get("oil", {})

    # Oil is lane-independent (same global price for every lane), looked up per date.
    oil_series = oil.get("series", {}) if oil.get("ok") else {}

    touched = 0
    if fx.get("ok"):
        for lane_key, meta in LANES.items():
            ccy = meta["currency"]
            ccy_series = fx["series"].get(ccy, {})
            for day_str, rate in ccy_series.items():
                save_snapshot(
                    conn,
                    snapshot_date=day_str,
                    lane=lane_key,
                    fx_rate=rate,
                    # nearest-available global oil price for that date (may be None
                    # if markets were closed that day; that's fine).
                    oil_price=oil_series.get(day_str),
                )
                touched += 1
    return touched


if __name__ == "__main__":
    # Smoke test: pull live data, backfill the DB, and read one lane back.
    from data_sources import fetch_all

    print("Fetching live data and backfilling SQLite store...")
    bundle = fetch_all()

    with get_connection() as conn:
        n = backfill_market_history(conn, bundle)
        print(f"Backfilled {n} (date, lane) rows into {DB_PATH.name}")

        sample = load_history(conn, "US", limit=5)
        print(f"\nLast {len(sample)} US rows:")
        for r in sample:
            print(f"  {r['date']}  fx={r['fx_rate']}  oil={r['oil_price']}")
