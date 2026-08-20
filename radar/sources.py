"""One function per site. No API keys -- everything here is public HTML or a
public JSON endpoint the site's own frontend calls.

Every fetcher follows the same contract: return a list of Event, or raise. The
caller catches per source, so one broken scraper never takes the run down.

The parsing style is deliberately loose. Instead of walking a brittle path like
data["props"]["pageProps"]["initialData"]["events"], we walk the whole JSON blob
and pick out any dict that *looks* like an event. Sites reshuffle their state
tree constantly; they rename the leaf keys far less often.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .model import Event

log = logging.getLogger("radar.sources")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

TIMEOUT = 25
RETRIES = 3


def get(url: str, params: Optional[dict] = None, as_json: bool = False, tries: int = RETRIES):
    """GET with retries on rate limits and server errors."""
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code} from {url}")
            resp.raise_for_status()
            return resp.json() if as_json else resp.text
        except Exception as exc:  # noqa: BLE001 -- retry anything transient
            last = exc
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError(f"failed to fetch {url}")


# ---------------------------------------------------------------- JSON helpers


def _iter_dicts(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_dicts(value)


def _brace_match(text: str, start: int) -> Optional[str]:
    """Slice out one balanced {...} starting at `start`, ignoring braces in strings."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _next_data(html: str) -> Optional[Any]:
    """Next.js page state. Covers both the __NEXT_DATA__ script tag and the
    newer streamed self.__next_f payloads."""
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # App-router streaming: many small pushes, each a JSON string containing JSON.
    chunks: List[Any] = []
    for raw in re.findall(r'self\.__next_f\.push\(\[\d+,\s*(".*?")\]\)', html, re.S):
        try:
            inner = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for m in re.finditer(r"[\[{]", inner):
            piece = _brace_match(inner, m.start()) if inner[m.start()] == "{" else None
            if piece and len(piece) > 200:
                try:
                    chunks.append(json.loads(piece))
                except json.JSONDecodeError:
                    continue
    return chunks or None


def _var_json(html: str, *names: str) -> Optional[Any]:
    """Pull an inline `window.NAME = {...}` assignment."""
    for name in names:
        match = re.search(re.escape(name) + r"\s*=\s*", html)
        if not match:
            continue
        brace = html.find("{", match.end())
        if brace == -1:
            continue
        blob = _brace_match(html, brace)
        if not blob:
            continue
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    return None


def _date(value: Any) -> Optional[Any]:
    if not value or not isinstance(value, str):
        return None
    try:
        return dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None


def _text(value: Any) -> str:
    """Sites variously give a plain string or {"text": ..., "html": ...}."""
    if isinstance(value, str):
        return html_lib.unescape(value)
    if isinstance(value, dict):
        for key in ("text", "value", "plainText", "name"):
            if isinstance(value.get(key), str):
                return html_lib.unescape(value[key])
    return ""


def _clean(html_or_text: str, limit: int = 600) -> str:
    """Strip markup and decode entities -- JSON-LD routinely ships both, so
    titles arrive as 'Support &amp; Strategy' unless we unescape them."""
    if not html_or_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = html_lib.unescape(text)
    text = " ".join(text.split())
    return text[:limit]


# ------------------------------------------------------------------ schema.org


def _price_from_offers(offers: Any) -> tuple[str, Optional[bool]]:
    items = offers if isinstance(offers, list) else [offers]
    prices: List[float] = []
    currency = "INR"
    for offer in items:
        if not isinstance(offer, dict):
            continue
        currency = offer.get("priceCurrency") or currency
        raw = offer.get("price", offer.get("lowPrice"))
        if raw in (None, ""):
            continue
        try:
            prices.append(float(str(raw).replace(",", "").replace("₹", "").strip()))
        except ValueError:
            continue
    if not prices:
        return "", None
    low = min(prices)
    if low <= 0:
        return "Free", True
    symbol = "₹" if currency in ("INR", "inr") else f"{currency} "
    return f"{symbol}{int(low):,}", False


def jsonld_events(html: str, source: str, default_city: str = "") -> List[Event]:
    soup = BeautifulSoup(html, "html.parser")
    blobs: List[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            blobs.append(json.loads(raw))
        except json.JSONDecodeError:
            # Some sites concatenate objects without wrapping them in an array.
            for match in re.finditer(r"\{", raw):
                piece = _brace_match(raw, match.start())
                if piece:
                    try:
                        blobs.append(json.loads(piece))
                    except json.JSONDecodeError:
                        pass
                    break

    out: List[Event] = []
    for blob in blobs:
        for node in _iter_dicts(blob):
            types = node.get("@type") or node.get("type") or ""
            types = types if isinstance(types, list) else [types]
            if not any("event" in str(t).lower() for t in types):
                continue
            name = _text(node.get("name"))
            url = node.get("url") or node.get("@id") or ""
            start = _date(node.get("startDate"))
            if not name or not url or start is None:
                continue

            location = node.get("location")
            venue, city = "", default_city
            for loc in _iter_dicts(location) if location else []:
                venue = venue or _text(loc.get("name"))
                address = loc.get("address")
                if isinstance(address, str):
                    city = city or address
                elif isinstance(address, dict):
                    city = _text(address.get("addressLocality")) or city
                    venue = venue or _text(address.get("streetAddress"))
            if isinstance(location, str):
                venue = venue or location

            price, is_free = _price_from_offers(node.get("offers"))
            mode = str(node.get("eventAttendanceMode") or "").lower()

            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")

            out.append(
                Event(
                    title=name,
                    url=url if str(url).startswith("http") else "",
                    source=source,
                    start=start,
                    end=_date(node.get("endDate")),
                    venue=_clean(venue, 160),
                    city=_clean(city, 80) or default_city,
                    price=price,
                    is_free=is_free,
                    is_online="online" in mode,
                    description=_clean(_text(node.get("description"))),
                    image=image if isinstance(image, str) else "",
                )
            )
    return [e for e in out if e.url]


# ------------------------------------------------------------------------ Luma


def _luma_event(node: dict, default_city: str) -> Optional[Event]:
    inner = node.get("event") if isinstance(node.get("event"), dict) else node
    name = _text(inner.get("name"))
    start = _date(inner.get("start_at") or inner.get("start_at_utc"))
    if not name or start is None:
        return None

    slug = inner.get("url") or inner.get("api_id") or ""
    if not slug:
        return None
    url = slug if str(slug).startswith("http") else f"https://lu.ma/{slug}"

    geo = inner.get("geo_address_info") or {}
    city = _text(geo.get("city")) or _text(geo.get("city_state")) or default_city
    venue = " ".join(
        x for x in (
            _text(geo.get("place_name")) or _text(geo.get("address")) or _text(geo.get("full_address")),
            _text(geo.get("sublocality")),
        ) if x
    )
    # The host calendar's name is real signal -- "AI Delhi", "Hardware Club" --
    # and it's the only text the city page gives us besides the title.
    calendar = _text((node.get("calendar") or {}).get("name")) if isinstance(node.get("calendar"), dict) else ""
    if calendar.lower() in ("personal", ""):
        calendar = ""

    ticket = node.get("ticket_info") or inner.get("ticket_info") or {}
    is_free = ticket.get("is_free")
    price = ""
    cents = ticket.get("price", {}) if isinstance(ticket.get("price"), dict) else {}
    if cents.get("cents"):
        price = f"₹{int(cents['cents']) // 100:,}" if cents.get("currency", "").lower() == "inr" \
            else f"{cents.get('currency', '').upper()} {int(cents['cents']) // 100:,}"

    return Event(
        title=name,
        url=url,
        source="luma",
        start=start,
        end=_date(inner.get("end_at")),
        venue=_clean(venue, 160),
        city=_clean(city, 80),
        country=_text(geo.get("country_code")) or _text(geo.get("country")),
        price=price,
        is_free=bool(is_free) if is_free is not None else None,
        # location_type is authoritative. virtual_info is present as a stub
        # ({"has_access": false}) on offline events too, so it can't be used
        # as a boolean -- doing so marks every Luma event online.
        is_online=str(inner.get("location_type") or "").lower() == "online",
        description=_clean(
            " · ".join(
                x for x in (
                    _text(inner.get("one_liner")),
                    _text(inner.get("description_short")),
                    f"Hosted by {calendar}" if calendar else "",
                ) if x
            )
        ),
        image=inner.get("cover_url") or "",
    )


def luma(city_slug: str) -> List[Event]:
    """Luma's own discover endpoint, falling back to the city page's embedded JSON."""
    events: List[Event] = []
    errors: List[str] = []

    for endpoint, params in (
        ("https://api.lu.ma/discover/get-page", {"slug": city_slug}),
        ("https://api.lu.ma/discover/city/get-events", {"slug": city_slug, "period": "future"}),
    ):
        try:
            data = get(endpoint, params=params, as_json=True, tries=2)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint.rsplit('/', 1)[-1]}: {exc}")
            continue
        for node in _iter_dicts(data):
            ev = _luma_event(node, city_slug)
            if ev:
                events.append(ev)
        if events:
            return _dedupe_local(events)

    # Fallback: the city page ships the same objects in its page state.
    html = get(f"https://lu.ma/{city_slug}")
    data = _next_data(html)
    if data:
        for node in _iter_dicts(data):
            ev = _luma_event(node, city_slug)
            if ev:
                events.append(ev)
    if not events:
        events = jsonld_events(html, "luma", city_slug)
    if not events and errors:
        raise RuntimeError("; ".join(errors[:2]))
    return _dedupe_local(events)


_META_DESC = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\'](.*?)["\']',
    re.S | re.I,
)


def enrich_descriptions(
    events: List[Event], cache: Optional[dict] = None, limit: int = 160
) -> Dict[str, str]:
    """Some listing pages ship a title and nothing else, which starves the
    scorer -- a BCI workshop and a run club look identical at that point. Pull
    each event page's meta description to fill the gap.

    `cache` is url -> description from previous runs; anything already in it is
    applied without a request. Returns only the newly fetched entries, for the
    caller to persist. Blank results are returned too, so a page with nothing
    usable is never asked twice.
    """
    cache = cache or {}
    fetched: Dict[str, str] = {}

    for ev in events:
        if ev.description:
            continue
        if ev.url in cache:
            ev.description = cache[ev.url]
            continue
        if len(fetched) >= limit:
            continue
        try:
            page = get(ev.url, tries=1)
        except Exception as exc:  # noqa: BLE001 -- a missing page is not fatal
            log.debug("  enrich %s: %s", ev.url, str(exc)[:60])
            continue

        match = _META_DESC.search(page)
        text = _clean(match.group(1)) if match else ""
        # Luma pads short descriptions with boilerplate about the host.
        if text.lower().startswith("join us on luma"):
            text = ""
        ev.description = text
        fetched[ev.url] = text

    return fetched


# ---------------------------------------------------------------------- Meetup


def _meetup_event(node: dict, country_hint: str = "") -> Optional[Event]:
    title = _text(node.get("title"))
    url = node.get("eventUrl") or node.get("url") or ""
    start = _date(node.get("dateTime") or node.get("startTime") or node.get("startDate"))
    if not title or not str(url).startswith("http") or start is None:
        return None

    venue_node = node.get("venue") if isinstance(node.get("venue"), dict) else {}
    group = node.get("group") if isinstance(node.get("group"), dict) else {}
    city = _text(venue_node.get("city")) or _text(group.get("city"))

    fee = node.get("feeSettings")
    is_free = node.get("isFree")
    if is_free is None:
        is_free = fee in (None, {}) if "feeSettings" in node else None
    price = ""
    if isinstance(fee, dict) and fee.get("amount"):
        price = f"{fee.get('currency', '')} {fee['amount']}".strip()

    event_type = str(node.get("eventType") or "").lower()
    return Event(
        title=title,
        url=url,
        source="meetup",
        start=start,
        end=_date(node.get("endTime")),
        venue=_clean(_text(venue_node.get("name")) or _text(group.get("name")), 160),
        city=_clean(city, 80),
        country=_text(venue_node.get("country")) or country_hint,
        price=price,
        is_free=bool(is_free) if is_free is not None else None,
        is_online="online" in event_type or bool(node.get("isOnline")),
        description=_clean(_text(node.get("description"))),
        image=(node.get("image") or {}).get("baseUrl", "") if isinstance(node.get("image"), dict) else "",
    )


def meetup(keyword: str, lat: float, lon: float, radius_miles: int) -> List[Event]:
    html = get(
        "https://www.meetup.com/find/",
        params={
            "keywords": keyword,
            "lat": lat,
            "lon": lon,
            "distance": f"{radius_miles}miles",
            "source": "EVENTS",
            "sortField": "DATETIME",
        },
    )
    data = _next_data(html)
    events: List[Event] = []
    if data:
        for node in _iter_dicts(data):
            ev = _meetup_event(node, country_hint="IN")
            if ev:
                events.append(ev)
    if not events:
        events = jsonld_events(html, "meetup")
    return _dedupe_local(events)


# ------------------------------------------------------------------ Eventbrite


def _eventbrite_event(node: dict, default_city: str, country_hint: str = "") -> Optional[Event]:
    name = _text(node.get("name"))
    url = node.get("url") or node.get("tracking_url") or ""
    start = _date(
        node.get("start_date")
        and f"{node.get('start_date')}T{node.get('start_time') or '00:00'}"
        or _text((node.get("start") or {}).get("local") if isinstance(node.get("start"), dict) else None)
    )
    if not name or not str(url).startswith("http") or start is None:
        return None

    venue_node = node.get("primary_venue") or node.get("venue") or {}
    address = venue_node.get("address") if isinstance(venue_node, dict) else {}
    address = address if isinstance(address, dict) else {}

    ticket = node.get("ticket_availability") or {}
    is_free = node.get("is_free")
    if is_free is None and isinstance(ticket, dict):
        is_free = ticket.get("is_free")
    price = ""
    if isinstance(ticket, dict):
        low = ticket.get("minimum_ticket_price") or {}
        if isinstance(low, dict) and low.get("display"):
            price = low["display"]

    return Event(
        title=name,
        url=url.split("?")[0],
        source="eventbrite",
        start=start,
        end=_date(
            node.get("end_date")
            and f"{node.get('end_date')}T{node.get('end_time') or '00:00'}"
            or None
        ),
        venue=_clean(_text(venue_node.get("name")) if isinstance(venue_node, dict) else "", 160),
        city=_clean(_text(address.get("city")) or default_city, 80),
        country=_text(address.get("country")) or country_hint,
        price=price,
        is_free=bool(is_free) if is_free is not None else None,
        is_online=bool(node.get("is_online_event")) or str(node.get("event_type", "")).lower() == "online",
        description=_clean(_text(node.get("summary")) or _text(node.get("description"))),
        image=((node.get("image") or {}).get("url", "") if isinstance(node.get("image"), dict) else ""),
    )


def eventbrite(place: str, query: str) -> List[Event]:
    slug = query.strip().lower().replace(" ", "-")
    html = get(f"https://www.eventbrite.com/d/{place}/{slug}/")
    data = _var_json(html, "window.__SERVER_DATA__", "__SERVER_DATA__")
    events: List[Event] = []
    city = place.split("--")[-1].replace("-", " ")
    country = "IN" if place.lower().startswith("india") else ""
    if data:
        for node in _iter_dicts(data):
            ev = _eventbrite_event(node, city, country_hint=country)
            if ev:
                events.append(ev)
    if not events:
        events = jsonld_events(html, "eventbrite", city)
    return _dedupe_local(events)


# --------------------------------------------------- schema.org-only platforms


def allevents(city: str, category: str = "technology") -> List[Event]:
    """Category paths vary by city -- some have /technology, some only
    /science-tech, and the miss is a redirect to a 404 page rather than a 404."""
    errors: List[str] = []
    for path in (category, "science-tech", "business"):
        try:
            html = get(f"https://allevents.in/{city}/{path}", tries=2)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {str(exc)[:60]}")
            continue
        found = jsonld_events(html, "allevents", city)
        for ev in found:
            ev.country = ev.country or "IN"
        if found:
            return _dedupe_local(found)
    if errors:
        raise RuntimeError("; ".join(errors))
    return []


def townscript(city: str = "india", category: str = "technology-events") -> List[Event]:
    html = get(f"https://www.townscript.com/{city}/{category}")
    return _dedupe_local(jsonld_events(html, "townscript", "" if city == "india" else city))


def devfolio(size: int = 100) -> List[Event]:
    """Devfolio renders its listings client-side, so the HTML is useless. The
    search API its own frontend calls is open, but only `type: upcoming`
    narrows the index -- status/sort/filter params are silently ignored.
    """
    resp = requests.post(
        "https://api.devfolio.co/api/search/hackathons",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": "", "from": 0, "size": size, "type": "upcoming"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    hits = (resp.json().get("hits") or {}).get("hits") or []

    events: List[Event] = []
    for hit in hits:
        node = hit.get("_source") or {}
        name, slug = _text(node.get("name")), node.get("slug")
        start = _date(node.get("starts_at"))
        if not name or not slug or start is None:
            continue
        online = bool(node.get("is_online"))
        city = _text(node.get("city")) or _text(node.get("location"))
        events.append(
            Event(
                title=name,
                url=f"https://{slug}.devfolio.co",
                source="devfolio",
                start=start,
                end=_date(node.get("ends_at")),
                venue=_clean(_text(node.get("location")), 160),
                city=_clean(city if not online else city or "Online", 80),
                country=_text(node.get("country")) or ("IN" if node.get("timezone", "").endswith(("Calcutta", "Kolkata")) else ""),
                is_free=True,  # Devfolio hackathons don't charge entry.
                is_online=online,
                description=_clean(_text(node.get("desc")) or _text(node.get("tagline"))),
                image=node.get("cover_img") or "",
            )
        )
    return _dedupe_local(events)


def commudle() -> List[Event]:
    """Angular app with no server-rendered event data -- kept for the day they
    add JSON-LD, but it returns nothing today."""
    html = get("https://www.commudle.com/explore")
    return _dedupe_local(jsonld_events(html, "commudle"))


# ---------------------------------------------------------------------- curated


def curated(entries: List[dict]) -> List[Event]:
    """Hand-verified events from config.yaml.

    The deep-tech conferences that matter most here are the least scrapable:
    10times and embs.org block automated requests outright, and KonfHub, IIT
    Delhi and NASSCOM ship no structured event data. Their dates are announced
    a year ahead and barely move, so typing them in once a year beats
    maintaining a scraper that breaks silently.
    """
    events: List[Event] = []
    for entry in entries or []:
        name, url = entry.get("name"), entry.get("url")
        start = _date(str(entry.get("start"))) if entry.get("start") else None
        if not name or not url or start is None:
            log.warning("  curated entry skipped (needs name, url, start): %r", name or entry)
            continue
        events.append(
            Event(
                title=name,
                url=url,
                source="curated",
                start=start,
                end=_date(str(entry["end"])) if entry.get("end") else None,
                venue=_clean(str(entry.get("venue") or ""), 160),
                city=_clean(str(entry.get("city") or ""), 80),
                country=str(entry.get("country") or "IN"),
                # `topic` exists so the scorer can see what the event is about;
                # a conference title alone rarely says.
                description=_clean(str(entry.get("topic") or entry.get("note") or "")),
                is_free=entry.get("free"),
            )
        )
    return events


# ----------------------------------------------------------------------- utils


def _dedupe_local(events: Iterable[Event]) -> List[Event]:
    """Cheap within-source dedupe -- the same event often appears in a page's
    state tree more than once (featured carousel plus the main list)."""
    seen: Dict[str, Event] = {}
    for ev in events:
        key = f"{ev.url}|{ev.day}"
        if key not in seen:
            seen[key] = ev
    return list(seen.values())
