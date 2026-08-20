"""Orchestration: scan every source, filter, dedupe, score, store, render, email.

Each fetcher is called inside its own try/except. A source that changed its
markup overnight logs an error and the run continues -- a partial page beats no
page, and the footer says which sources went quiet.
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
from collections import Counter
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import yaml

from . import db, dedupe, render, score, sources, trust
from .model import Event, now_ist

log = logging.getLogger("radar")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

# Fallback if config.yaml has no search_terms.
DEFAULT_TERMS = [
    "artificial intelligence", "machine learning", "deep learning", "hardware",
    "robotics", "startup founders", "neurotech", "medical devices",
]


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def collect(cfg: dict) -> Tuple[List[Event], List[str], int]:
    """Run every fetcher. Returns (events, names_that_failed, attempts)."""
    home = cfg.get("home") or {}
    india = cfg.get("india") or {}
    terms = cfg.get("search_terms") or DEFAULT_TERMS

    jobs: List[Tuple[str, Callable[[], List[Event]]]] = []

    for slug in home.get("luma_city_slugs") or []:
        jobs.append((f"luma/{slug}", lambda s=slug: sources.luma(s)))
    for slug in india.get("luma_city_slugs") or []:
        jobs.append((f"luma/{slug}", lambda s=slug: sources.luma(s)))

    lat = home.get("meetup_lat", 28.4595)
    lon = home.get("meetup_lon", 77.0266)
    radius = int(home.get("meetup_radius_miles", 40))
    for term in terms:
        jobs.append(
            (f"meetup/{term}", lambda t=term: sources.meetup(t, lat, lon, radius))
        )

    for place in india.get("eventbrite_places") or []:
        for term in ("technology", "ai", "startup"):
            jobs.append(
                (f"eventbrite/{place}/{term}", lambda p=place, t=term: sources.eventbrite(p, t))
            )

    for city in ("gurgaon", "delhi", "bangalore", "mumbai", "hyderabad", "pune"):
        jobs.append((f"allevents/{city}", lambda c=city: sources.allevents(c)))

    jobs.append(("devfolio", sources.devfolio))
    # Townscript and Commudle render their listings client-side and serve no
    # usable JSON-LD, so calling them only adds noise to the log. The fetchers
    # stay in sources.py for the day that changes.

    events: List[Event] = []
    failed: List[str] = []
    for name, fetch in jobs:
        try:
            found = fetch()
        except Exception as exc:  # noqa: BLE001 -- one bad source must not stop the run
            log.warning("  %-34s failed: %s", name, str(exc)[:120])
            failed.append(name)
            continue
        log.info("  %-34s %d", name, len(found))
        events.extend(found)

    return events, failed, len(jobs)


def in_horizon(ev: Event, days: int) -> bool:
    if ev.start is None:
        return False
    now = now_ist()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today <= ev.start <= now + timedelta(days=days)


def rejection_line(counter: Counter) -> str:
    if not counter:
        return ""
    parts = "; ".join(f"{reason}×{n}" for reason, n in counter.most_common())
    return f"{sum(counter.values())} untrusted ({parts})"


def run(no_fetch: bool = False) -> int:
    cfg = load_config()
    out_dir = ROOT / (cfg.get("output") or {}).get("site_dir", "site")
    horizon = int((cfg.get("output") or {}).get("digest_days", 60))
    run_ts = now_ist().isoformat()

    conn = db.connect(ROOT / "events.db")
    stats: Dict = {"sources_tried": 0, "sources_ok": 0, "raw": 0, "failed_sources": []}

    if no_fetch:
        kept_events = db.load_upcoming(conn, horizon)
        log.info("--no-fetch: rebuilding from %d stored events", len(kept_events))
        stats["raw"] = len(kept_events)
        stats["after_dedupe"] = len(kept_events)
        merged = kept_events
        rejected: Counter = Counter()
    else:
        log.info("Scanning sources…")
        raw, failed, attempts = collect(cfg)
        stats.update(
            sources_tried=attempts,
            sources_ok=attempts - len(failed),
            raw=len(raw),
            failed_sources=failed,
        )

        upcoming = [e for e in raw if in_horizon(e, horizon)]
        log.info("%d listings, %d inside the %d-day window", len(raw), len(upcoming), horizon)

        rejected = Counter()
        trusted: List[Event] = []
        for ev in upcoming:
            ok, reason = trust.check(ev)
            if ok:
                ev.trust = trust.base_trust(ev)
                trusted.append(ev)
            else:
                rejected[reason] += 1
        if rejected:
            log.info("%s", rejection_line(rejected))

        merged = dedupe.merge(trusted)
        stats["after_dedupe"] = len(merged)
        log.info("%d trusted, %d after dedupe", len(trusted), len(merged))

        # Fill in descriptions for listings that arrived with a bare title.
        # Runs after dedupe so we never fetch the same event twice, and reuses
        # the cache so a daily run only pays for genuinely new listings.
        cache = db.description_cache(conn)
        fetched = sources.enrich_descriptions(merged, cache=cache)
        if fetched:
            db.save_descriptions(conn, fetched, run_ts)
        log.info(
            "descriptions: %d fetched, %d reused from cache",
            len(fetched), sum(1 for e in merged if e.url in cache),
        )

    # Scoring happens on every run, including --no-fetch, so config edits show up.
    kept = [ev for ev in merged if score.apply(ev, cfg)]
    for ev in kept:
        if not ev.eid:
            ev.eid = dedupe.stable_id(ev)
    stats["kept"] = len(kept)
    stats["rejected_line"] = rejection_line(rejected)

    if no_fetch:
        new_count = sum(1 for e in kept if e.is_new)
    else:
        new_count = db.sync(conn, kept, run_ts)
        removed = db.prune(conn) + db.prune_descriptions(conn)
        if removed:
            log.info("pruned %d stale rows from the db", removed)
    stats["new_count"] = new_count

    by_region = Counter(e.region for e in kept)
    log.info(
        "kept %d (NCR %d · India %d · global %d) · %d new",
        len(kept), by_region["home"], by_region["india"], by_region["global"], new_count,
    )

    page = render.build(kept, cfg, stats, out_dir)
    log.info("wrote %s", page.relative_to(ROOT))

    if not no_fetch:
        maybe_email([e for e in kept if e.is_new])

    conn.close()
    return 0


def maybe_email(new_events: List[Event]) -> None:
    user, password, to = (
        os.environ.get("SMTP_USER"),
        os.environ.get("SMTP_PASS"),
        os.environ.get("DIGEST_TO"),
    )
    if not (user and password and to):
        return
    if not new_events:
        log.info("no new events, skipping the digest email")
        return

    lines = []
    for ev in sorted(new_events, key=lambda e: (e.region != "home", -e.score)):
        when = ev.start.strftime("%a %d %b, %H:%M") if ev.start else "TBA"
        where = " · ".join(x for x in (ev.venue, ev.city) if x) or ("Online" if ev.is_online else "")
        lines.append(
            f"[{ev.score:>2}] {ev.title}\n"
            f"      {when} · {where} · {ev.price_label()}\n"
            f"      {ev.url}"
        )

    msg = EmailMessage()
    msg["Subject"] = f"Event Radar — {len(new_events)} new"
    msg["From"] = user
    msg["To"] = to
    msg.set_content("\n\n".join(lines) + "\n")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
        log.info("emailed %d new events to %s", len(new_events), to)
    except Exception as exc:  # noqa: BLE001 -- a failed email must not fail the run
        log.warning("digest email failed: %s", exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scan event sites and rebuild the page.")
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="rebuild the page from the stored database without hitting the network",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    return run(no_fetch=args.no_fetch)


if __name__ == "__main__":
    raise SystemExit(main())
