"""Relevance scoring and region assignment.

A term match adds its bucket's weight. Matches per bucket are capped at three,
so a listing that stuffs twenty keywords into its description doesn't outrank a
real BCI workshop. Free adds +1 (no reason not to go); online subtracts 2
(the point is being in the room).
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .model import Event

MATCHES_PER_BUCKET = 3

# Words that describe an event's *format*, not its subject. They still add their
# bucket's points, but they can never be the only reason an event qualifies.
FORMAT_TERMS = {
    "workshop", "conference", "summit", "meetup", "symposium", "webinar",
    "session", "event", "bootcamp", "talk", "panel", "expo", "conclave",
}

# Bucket -> the tag shown on the page. Drives the category filter chips.
BUCKET_TAGS = {
    "core": "BCI / Medtech",
    "strong": "Hardware / ML",
    "network": "Founders",
}

# Finer tags, checked after scoring, purely for the UI filters.
TAG_RULES = (
    ("BCI / Neuro", ("bci", "brain computer interface", "brain-computer", "neurotech",
                     "neurotechnology", "eeg", "emg", "fnirs", "neural interface",
                     "neuromodulation", "neuroscience", "neuralink", "electrophysiology")),
    ("Medical devices", ("medical device", "medtech", "digital health", "implant",
                         "prosthetic", "bioelectronics", "biosignal", "biosensing",
                         "wearable", "ppg", "clinical")),
    ("Hardware", ("hardware", "embedded", "silicon", "semiconductor", "pcb", "fpga",
                  "mems", "photonics", "chip", "firmware", "sensors", "rf", "radar",
                  "robotics", "edge ai")),
    ("AI / ML", ("ai", "artificial intelligence", "machine learning", "deep learning",
                 "llm", "transformer", "foundation model", "computer vision",
                 "multimodal", "mlops", "ml systems", "pytorch", "cuda", "speech",
                 "signal processing")),
    ("Founders", ("founder", "founders", "startup", "yc", "y combinator", "demo day",
                  "pitch", "vc", "venture", "angel", "accelerator", "incubator",
                  "seed round", "builders")),
    ("Hackathon", ("hackathon", "hack day", "buildathon")),
)

_word_cache: Dict[str, re.Pattern] = {}


def _pattern(term: str) -> re.Pattern:
    """Word-boundary match so 'ai' doesn't fire inside 'chair' or 'email'."""
    if term not in _word_cache:
        _word_cache[term] = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
    return _word_cache[term]


def _matched_terms(hay: str, terms) -> List[str]:
    return [t for t in terms if _pattern(t.lower()).search(hay)]


def relevance(event: Event, cfg: dict) -> Tuple[int, List[str], List[str]]:
    """Returns (score, ui_tags, topical_terms_matched).

    Title matches count for the bucket's full weight; description-only matches
    count for roughly half. An event that is actually about hardware says so in
    its title -- "India Furniture Conclave" only mentions hardware deep in its
    blurb (furniture hardware), and "Theatre Workshop" matched "speech" because
    the description talks about public speaking. Weighting by position drops
    both without needing a per-word exception list.
    """
    title_hay = f"{event.title} {event.venue}".lower()
    desc_hay = event.description.lower()

    total = 0
    matched_all: List[str] = []

    for bucket in (cfg.get("keywords") or {}).values():
        weight = int(bucket.get("weight", 1))
        terms = bucket.get("terms") or []

        in_title = _matched_terms(title_hay, terms)
        in_desc = [t for t in _matched_terms(desc_hay, terms) if t not in in_title]

        total += weight * min(len(in_title), MATCHES_PER_BUCKET)
        if in_desc:
            total += max(1, weight // 2) * min(len(in_desc), MATCHES_PER_BUCKET)
        matched_all.extend(in_title + in_desc)

    if event.is_free:
        total += 1
    if event.is_online:
        total -= 2

    hay = f"{title_hay} {desc_hay}"
    tags: List[str] = [tag for tag, terms in TAG_RULES if _matched_terms(hay, terms)]

    # Terms that say what the event is *about*, as opposed to what format it is.
    topical = [t for t in matched_all if t.lower() not in FORMAT_TERMS]
    if not tags:
        tags = ["Other"]

    return max(total, 0), tags, topical


def blocked(event: Event, cfg: dict) -> bool:
    hay = event.haystack
    return any(term.lower() in hay for term in (cfg.get("blocklist") or []))


# Cities that count as "rest of India" when a source doesn't tell us the country.
INDIA_CITIES = {
    "bangalore", "bengaluru", "mumbai", "bombay", "hyderabad", "pune", "chennai",
    "madras", "kolkata", "calcutta", "ahmedabad", "goa", "jaipur", "kochi", "cochin",
    "indore", "chandigarh", "lucknow", "bhopal", "nagpur", "surat", "coimbatore",
    "thiruvananthapuram", "trivandrum", "mysore", "mysuru", "vizag", "visakhapatnam",
    "bhubaneswar", "guwahati", "dehradun", "kanpur", "patna", "raipur", "ranchi",
    "vadodara", "rajkot", "mangalore", "mangaluru", "nashik", "aurangabad", "udaipur",
    "gandhinagar", "roorkee", "kharagpur", "kanpur nagar", "india",
}


def region_of(event: Event, cfg: dict) -> str:
    """'home' (NCR), 'india', or 'global'."""
    home_names = [c.lower() for c in (cfg.get("home") or {}).get("city_names") or []]
    blob = f"{event.city} {event.venue}".lower()

    for name in home_names:
        if _pattern(name).search(blob):
            return "home"

    # A country code from the source is more reliable than matching city names.
    if event.country.upper() in ("IN", "IND", "INDIA"):
        return "india"

    for name in INDIA_CITIES:
        if _pattern(name).search(blob):
            return "india"

    india_slugs = [s.lower() for s in (cfg.get("india") or {}).get("luma_city_slugs") or []]
    for name in india_slugs:
        if _pattern(name).search(blob):
            return "india"

    return "global"


def apply(event: Event, cfg: dict) -> bool:
    """Score, tag and place the event. False means drop it."""
    if blocked(event, cfg):
        return False

    event.score, event.tags, topical = relevance(event, cfg)
    event.region = region_of(event, cfg)

    # Format words alone say nothing about subject. Without this, a "Theatre
    # Workshop" clears the local threshold on the word "workshop".
    if not topical:
        return False

    threshold = int((cfg.get("thresholds") or {}).get(event.region, 99))
    return event.score >= threshold
