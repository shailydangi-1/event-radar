"""The one shape every source produces.

Sources disagree about everything -- field names, date formats, whether "free"
is a boolean or the string "Free". They all normalise into this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    """Everything is compared and displayed in IST. Naive input is assumed IST
    already -- Indian listing sites publish local time without an offset."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


@dataclass
class Event:
    title: str
    url: str
    source: str

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    venue: str = ""
    city: str = ""
    # ISO country code when the source tells us. India has far more cities than
    # any hardcoded list will cover, so a country hint beats guessing by name.
    country: str = ""
    price: str = ""
    is_free: Optional[bool] = None
    is_online: bool = False
    description: str = ""
    image: str = ""

    # Filled in downstream, not by sources.
    eid: str = ""
    score: int = 0
    tags: List[str] = field(default_factory=list)
    region: str = ""
    also_on: List[str] = field(default_factory=list)
    trust: int = 0
    is_new: bool = False

    def __post_init__(self) -> None:
        self.title = " ".join((self.title or "").split())
        self.description = " ".join((self.description or "").split())
        self.start = to_ist(self.start)
        self.end = to_ist(self.end)

    @property
    def haystack(self) -> str:
        """What the scorer and the spam check read."""
        return f"{self.title}\n{self.description}\n{self.venue}".lower()

    @property
    def day(self) -> str:
        return self.start.strftime("%Y-%m-%d") if self.start else ""

    def price_label(self) -> str:
        if self.is_free:
            return "Free"
        if self.price:
            return self.price
        return "Paid" if self.is_free is False else "—"
