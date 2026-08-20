"""SQLite persistence.

Two jobs: remember what we've already seen so "new since last run" is accurate,
and let `--no-fetch` rebuild the page without touching the network. Scores are
deliberately *not* stored -- they're recomputed from config.yaml on every
render, so editing a threshold and rerunning with --no-fetch shows the effect.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

from dateutil import parser as dateparser

from .model import Event, now_ist

DB_PATH = Path("events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT NOT NULL,
    start       TEXT,
    end         TEXT,
    venue       TEXT,
    city        TEXT,
    country     TEXT,
    price       TEXT,
    is_free     INTEGER,
    is_online   INTEGER,
    description TEXT,
    image       TEXT,
    region      TEXT,
    also_on     TEXT,
    trust       INTEGER,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_start ON events (start);
-- Enrichment cache. Keyed by URL and kept for every event we *looked at*, not
-- just the ones that scored high enough to publish -- otherwise the ~95% of
-- listings that get filtered out are refetched every single morning. A blank
-- description is cached too: it records "we asked, there was nothing".
CREATE TABLE IF NOT EXISTS descriptions (
    url         TEXT PRIMARY KEY,
    description TEXT,
    fetched     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    ts        TEXT PRIMARY KEY,
    found     INTEGER,
    new_count INTEGER
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created, so an existing
    committed events.db keeps working instead of erroring on the next run."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    for column, ddl in (("country", "TEXT"),):
        if column not in have:
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} {ddl}")
    conn.commit()


def last_run(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT ts FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    return row["ts"] if row else None


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def sync(conn: sqlite3.Connection, events: List[Event], run_ts: str) -> int:
    """Upsert this run's events. Marks is_new on anything we hadn't seen before
    the previous run. Returns the count of new events."""
    previous = last_run(conn)
    new_count = 0

    for ev in events:
        row = conn.execute("SELECT first_seen FROM events WHERE id = ?", (ev.eid,)).fetchone()
        first_seen = row["first_seen"] if row else run_ts
        # Nothing seen before the first ever run counts as "new" -- otherwise the
        # first page would be a wall of badges.
        ev.is_new = bool(previous) and first_seen > previous
        if not row:
            new_count += 1

        conn.execute(
            """
            INSERT INTO events (id, title, url, source, start, end, venue, city, country,
                                price, is_free, is_online, description, image, region,
                                also_on, trust, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, url=excluded.url, source=excluded.source,
                start=excluded.start, end=excluded.end, venue=excluded.venue,
                city=excluded.city, country=excluded.country,
                price=excluded.price, is_free=excluded.is_free,
                is_online=excluded.is_online,
                description=CASE WHEN length(excluded.description) > length(COALESCE(events.description,''))
                                 THEN excluded.description ELSE events.description END,
                image=COALESCE(NULLIF(excluded.image,''), events.image),
                region=excluded.region, also_on=excluded.also_on,
                trust=excluded.trust, last_seen=excluded.last_seen
            """,
            (
                ev.eid, ev.title, ev.url, ev.source, _iso(ev.start), _iso(ev.end),
                ev.venue, ev.city, ev.country, ev.price,
                None if ev.is_free is None else int(ev.is_free), int(ev.is_online),
                ev.description, ev.image, ev.region, json.dumps(ev.also_on),
                ev.trust, first_seen, run_ts,
            ),
        )

    conn.execute(
        "INSERT OR REPLACE INTO runs (ts, found, new_count) VALUES (?,?,?)",
        (run_ts, len(events), new_count),
    )
    conn.commit()
    return new_count


def description_cache(conn: sqlite3.Connection) -> dict:
    """url -> description for every event page we've already fetched. Values may
    be empty strings, meaning the page had nothing usable; those still count as
    known so we don't ask again."""
    return {
        row["url"]: row["description"] or ""
        for row in conn.execute("SELECT url, description FROM descriptions")
    }


def save_descriptions(conn: sqlite3.Connection, fetched: dict, run_ts: str) -> None:
    conn.executemany(
        "INSERT INTO descriptions (url, description, fetched) VALUES (?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET description=excluded.description, fetched=excluded.fetched",
        [(url, text, run_ts) for url, text in fetched.items()],
    )
    conn.commit()


def prune_descriptions(conn: sqlite3.Connection, keep_days: int = 180) -> int:
    """Forget pages for events that are long past, so the cache doesn't grow
    without bound in a committed database."""
    cutoff = (now_ist() - timedelta(days=keep_days)).isoformat()
    cur = conn.execute("DELETE FROM descriptions WHERE fetched < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def prune(conn: sqlite3.Connection, keep_days: int = 120) -> int:
    """Drop events that finished a while ago. Keeps the committed db small."""
    cutoff = (now_ist() - timedelta(days=keep_days)).isoformat()
    cur = conn.execute("DELETE FROM events WHERE start IS NOT NULL AND start < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def load_upcoming(conn: sqlite3.Connection, days: int) -> List[Event]:
    """Rehydrate future events for a --no-fetch rebuild."""
    now = now_ist()
    horizon = (now + timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM events WHERE start IS NOT NULL AND start >= ? AND start <= ? ORDER BY start",
        (now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), horizon),
    ).fetchall()

    previous = conn.execute(
        "SELECT ts FROM runs ORDER BY ts DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    prev_ts = previous["ts"] if previous else None

    out: List[Event] = []
    for row in rows:
        ev = Event(
            title=row["title"],
            url=row["url"],
            source=row["source"],
            start=dateparser.parse(row["start"]) if row["start"] else None,
            end=dateparser.parse(row["end"]) if row["end"] else None,
            venue=row["venue"] or "",
            city=row["city"] or "",
            country=row["country"] or "",
            price=row["price"] or "",
            is_free=None if row["is_free"] is None else bool(row["is_free"]),
            is_online=bool(row["is_online"]),
            description=row["description"] or "",
            image=row["image"] or "",
        )
        ev.eid = row["id"]
        ev.region = row["region"] or ""
        ev.trust = row["trust"] or 0
        try:
            ev.also_on = json.loads(row["also_on"] or "[]")
        except json.JSONDecodeError:
            ev.also_on = []
        ev.is_new = bool(prev_ts) and row["first_seen"] > prev_ts
        out.append(ev)
    return out
