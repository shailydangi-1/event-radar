"""URL canonicalisation, fuzzy cross-source merge, and stable IDs.

Three layers, in order:

  1. Canonical URL -- the same link posted twice is one row.
  2. Fuzzy title match within the same day and metro -- catches the same event
     listed on Luma and Meetup under slightly different names.
  3. Stable ID -- built from the normalised title, so yesterday's row matches
     today's scan even when a different source wins the merge.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Dict, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .model import Event

# Which listing we'd rather send you to when the same event appears twice.
SOURCE_RANK = {
    "luma": 0,
    "meetup": 1,
    "eventbrite": 2,
    "devfolio": 2,
    "commudle": 2,
    "townscript": 3,
    "allevents": 4,
    "curated": 0,
}

TITLE_SIMILARITY = 0.82

# Delhi NCR is one metro -- an event tagged Gurugram on Luma and Delhi on Meetup
# is the same event, so they have to land in the same fuzzy-match bucket.
NCR = {
    "gurugram", "gurgaon", "delhi", "new delhi", "noida", "greater noida",
    "faridabad", "ghaziabad", "ncr", "delhi ncr", "sohna", "manesar",
}

TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_")
TRACKING_KEYS = {
    "ref", "fbclid", "gclid", "igshid", "mkt_tok", "aff", "affiliate",
    "source", "src", "referrer", "trk", "spm", "_ga", "gad_source",
}

# Noise that differs between listings of the same event.
_TITLE_NOISE = re.compile(
    r"\b("
    r"vol(ume)?\.?\s*\d+|no\.?\s*\d+|#\d+|part\s*\d+|ep(isode)?\.?\s*\d+|"
    r"\d{4}|\d+(st|nd|rd|th)|"
    r"edition|meetup|meet\s*up|event|webinar|workshop|session|"
    r"free|tickets?|registration|register|rsvp|online|virtual|"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(t|tember)?|oct(ober)?|nov(ember)?|dec(ember)?"
    r")\b",
    re.I,
)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    keep = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_KEYS
        and not any(k.lower().startswith(p) for p in TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(keep)), ""))


def norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[‘’“”]", "", t)
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    return " ".join(t.split())


def metro(event: Event) -> str:
    blob = f"{event.city} {event.venue}".lower()
    for name in NCR:
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", blob):
            return "ncr"
    return (event.city or "").strip().lower() or "unknown"


def stable_id(event: Event) -> str:
    key = "|".join([norm_title(event.title) or canonical_url(event.url), event.day, metro(event)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _better(a: Event, b: Event) -> Tuple[Event, Event]:
    """(winner, loser) -- best source wins, richer listing breaks the tie."""
    ra, rb = SOURCE_RANK.get(a.source, 9), SOURCE_RANK.get(b.source, 9)
    if ra != rb:
        return (a, b) if ra < rb else (b, a)
    fill_a = sum(bool(x) for x in (a.description, a.venue, a.image, a.price))
    fill_b = sum(bool(x) for x in (b.description, b.venue, b.image, b.price))
    return (a, b) if fill_a >= fill_b else (b, a)


def _absorb(winner: Event, loser: Event) -> Event:
    """Winner keeps its own link and title; blanks are backfilled from the loser."""
    for field_name in ("venue", "city", "price", "description", "image"):
        if not getattr(winner, field_name) and getattr(loser, field_name):
            setattr(winner, field_name, getattr(loser, field_name))
    if winner.start is None:
        winner.start = loser.start
    if winner.end is None:
        winner.end = loser.end
    if winner.is_free is None:
        winner.is_free = loser.is_free

    for src in [loser.source] + loser.also_on:
        if src != winner.source and src not in winner.also_on:
            winner.also_on.append(src)

    # Appearing on two independent platforms is corroboration.
    winner.trust = max(winner.trust, loser.trust) + 1
    return winner


def merge(events: List[Event]) -> List[Event]:
    # Layer 1: canonical URL.
    by_url: Dict[str, Event] = {}
    for ev in events:
        key = canonical_url(ev.url)
        if key in by_url:
            win, lose = _better(by_url[key], ev)
            by_url[key] = _absorb(win, lose)
        else:
            by_url[key] = ev

    # Layer 2: fuzzy title, bucketed by day + metro so we only compare events
    # that could plausibly be the same one.
    buckets: Dict[Tuple[str, str], List[Event]] = {}
    for ev in by_url.values():
        buckets.setdefault((ev.day, metro(ev)), []).append(ev)

    out: List[Event] = []
    for bucket in buckets.values():
        kept: List[Tuple[str, Event]] = []
        for ev in bucket:
            norm = norm_title(ev.title)
            hit = None
            for idx, (other_norm, other) in enumerate(kept):
                if not norm or not other_norm:
                    continue
                if SequenceMatcher(None, norm, other_norm).ratio() >= TITLE_SIMILARITY:
                    hit = idx
                    break
            if hit is None:
                kept.append((norm, ev))
            else:
                win, lose = _better(kept[hit][1], ev)
                kept[hit] = (norm_title(win.title), _absorb(win, lose))
        out.extend(ev for _, ev in kept)

    # Layer 3: stable IDs for the database.
    for ev in out:
        ev.eid = stable_id(ev)

    # Two different buckets can still collide on ID (same title, same day,
    # one with an unknown city). Collapse those too.
    by_id: Dict[str, Event] = {}
    for ev in out:
        if ev.eid in by_id:
            win, lose = _better(by_id[ev.eid], ev)
            merged = _absorb(win, lose)
            merged.eid = ev.eid
            by_id[ev.eid] = merged
        else:
            by_id[ev.eid] = ev

    return list(by_id.values())
