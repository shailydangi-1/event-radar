"""Domain allowlist plus junk-listing detection.

Two independent questions, both of which must pass:

  1. Is the host one we trust to list real events at all?
  2. Does *this particular listing* look real?

A good host isn't enough -- anyone can post anything to Eventbrite. And a clean
listing on a random blog isn't enough either, because we have no way to tell a
real workshop from an invented one. So: allowlist AND listing check.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

from .model import Event

# Platforms with real moderation, plus institutions that are credible by
# construction. Tier 1 listings are allowed to be sparse -- an IEEE workshop
# page with nothing but a title and a date is still a real IEEE workshop.
TIER1 = {
    "lu.ma",
    "luma.com",
    "meetup.com",
    "eventbrite.com",
    "eventbrite.co.uk",
    "eventbrite.in",
    "devfolio.co",
    "commudle.com",
    "konfhub.com",
    "unstop.com",
    "hackerearth.com",
    "ieee.org",
    "embs.org",
    "acm.org",
    "nasscom.in",
    "tie.org",
    "tiedelhi.org",
    "startupindia.gov.in",
    "neurips.cc",
    "icml.cc",
    "iclr.cc",
    "thecvf.com",
    "bcisociety.org",
    "sfn.org",
}

# Real platforms, looser moderation. Must have a clean, fleshed-out listing.
TIER2 = {
    "allevents.in",
    "townscript.com",
    "insider.in",
    "10times.com",
    "bookmyshow.com",
    "meraevents.com",
}

# Institutional suffixes -- universities, research labs, government.
ACADEMIC_SUFFIXES = (".ac.in", ".edu", ".edu.in", ".gov.in", ".res.in", ".nic.in", ".ac.uk")

# Wording that reliably marks a listing as lead-generation rather than an event.
SPAM_PHRASES = (
    "100% job guarantee",
    "job guarantee",
    "guaranteed placement",
    "whatsapp us",
    "whatsapp me",
    "dm for details",
    "earn 2 lakh",
    "earn upto",
    "earn up to rs",
    "work from home job",
    "passive income",
    "double your money",
    "limited seats hurry",
    "registration fee only",
    "become a millionaire",
    "no experience needed earn",
)


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _registrable(host: str) -> str:
    """Good enough for an allowlist check: keep the last two or three labels."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle co.uk / ac.in / gov.in / co.in style two-part public suffixes.
    if parts[-2] in {"co", "ac", "gov", "res", "nic", "org", "com", "net"} and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# Platforms that run a separate domain per country. Listing the ccTLDs by hand
# is a losing game -- eventbrite.com.au, .ie, .de, .sg all showed up in one run.
MULTI_TLD_TIER1 = {"eventbrite", "meetup"}


def tier(url: str) -> Optional[int]:
    host = host_of(url)
    if not host:
        return None
    if host.endswith(ACADEMIC_SUFFIXES):
        return 1
    if host.split(".")[0] in MULTI_TLD_TIER1:
        return 1
    reg = _registrable(host)
    for candidate in (host, reg):
        if candidate in TIER1:
            return 1
        if candidate in TIER2:
            return 2
    # Subdomains of allowlisted hosts, e.g. events.ieee.org
    for good in TIER1:
        if host.endswith("." + good):
            return 1
    for ok in TIER2:
        if host.endswith("." + ok):
            return 2
    return None


def _shouty(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 12:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > 0.85


def check(event: Event) -> Tuple[bool, str]:
    """Returns (keep, reason_if_dropped). The reason is what the run log prints."""
    # Curated entries were verified by hand when they were typed into
    # config.yaml, which is a stronger check than any allowlist. Their hosts are
    # conference sites -- semiconindia.org, medicalfair-india.com -- that would
    # otherwise be rejected as unknown.
    if event.source == "curated":
        return (True, "") if event.start else (False, "missing date")

    host = host_of(event.url)
    t = tier(event.url)
    if t is None:
        return False, f"unverified ({host or 'no host'})"

    hay = event.haystack
    for phrase in SPAM_PHRASES:
        if phrase in hay:
            return False, "spam wording"

    if _shouty(event.title):
        return False, "shouty title"

    if event.start is None:
        return False, "missing date"

    if t == 2:
        thin = (
            not event.venue
            and not event.is_online
            and not event.price
            and event.is_free is None
            and len(event.description) < 80
        )
        if thin:
            return False, "sparse listing"

    return True, ""


def base_trust(event: Event) -> int:
    """Starting trust score. Cross-posting adds to this during dedupe."""
    if event.source == "curated":
        return 3
    t = tier(event.url)
    return {1: 2, 2: 1}.get(t or 0, 0)
