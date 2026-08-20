"""Static page + RSS feed.

The page is one self-contained HTML file with the event list embedded as JSON.
Filtering happens client-side, so there's no server and no build step beyond
writing the file. Works opened from disk or served from GitHub Pages.
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List

from .model import Event, now_ist

REGION_TITLES = {
    "home": ("Delhi NCR", "Local — you can just show up"),
    "india": ("Rest of India", "Worth a flight"),
    "global": ("Global", "Only the genuinely major ones"),
}


def _event_json(ev: Event) -> dict:
    return {
        "id": ev.eid,
        "title": ev.title,
        "url": ev.url,
        "source": ev.source,
        "alsoOn": ev.also_on,
        "start": ev.start.isoformat() if ev.start else None,
        "startLabel": ev.start.strftime("%a %d %b · %H:%M") if ev.start else "Date TBA",
        "dayLabel": ev.start.strftime("%d %b") if ev.start else "TBA",
        "venue": ev.venue,
        "city": ev.city,
        "price": ev.price_label(),
        "isFree": bool(ev.is_free),
        "isOnline": ev.is_online,
        "description": ev.description[:280],
        "score": ev.score,
        "tags": ev.tags,
        "region": ev.region,
        "isNew": ev.is_new,
        "trust": ev.trust,
    }


def _watchlist_json(cfg: dict) -> List[dict]:
    out = []
    for item in cfg.get("global_watchlist") or []:
        out.append(
            {
                "name": item.get("name", ""),
                "url": item.get("url", ""),
                "note": item.get("note", ""),
                "month": item.get("month", ""),
            }
        )
    return out


def build(events: List[Event], cfg: dict, stats: Dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    max_per = int((cfg.get("output") or {}).get("max_per_section", 60))

    grouped: Dict[str, List[Event]] = {"home": [], "india": [], "global": []}
    for ev in sorted(events, key=lambda e: (e.start or now_ist(), -e.score)):
        if ev.region in grouped:
            grouped[ev.region].append(ev)

    sections = []
    for key in ("home", "india", "global"):
        title, subtitle = REGION_TITLES[key]
        sections.append(
            {
                "key": key,
                "title": title,
                "subtitle": subtitle,
                "threshold": (cfg.get("thresholds") or {}).get(key),
                "events": [_event_json(e) for e in grouped[key][:max_per]],
            }
        )

    payload = {
        "generated": now_ist().strftime("%d %b %Y, %H:%M IST"),
        "horizon": int((cfg.get("output") or {}).get("digest_days", 60)),
        "sections": sections,
        "watchlist": _watchlist_json(cfg),
        "stats": stats,
    }

    page = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    _write_rss(events, cfg, out_dir)
    return index


def _write_rss(events: List[Event], cfg: dict, out_dir: Path) -> Path:
    now = now_ist()
    items = []
    ranked = sorted(events, key=lambda e: (not e.is_new, -e.score, e.start or now))[:60]
    for ev in ranked:
        when = ev.start.strftime("%a %d %b %Y, %H:%M IST") if ev.start else "Date TBA"
        where = " · ".join(x for x in (ev.venue, ev.city) if x) or ("Online" if ev.is_online else "")
        body = (
            f"{when}{' · ' + where if where else ''} · {ev.price_label()} · "
            f"score {ev.score} · via {ev.source}"
            f"{'<br>' + html.escape(ev.description) if ev.description else ''}"
        )
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(ev.title)}</title>\n"
            f"      <link>{html.escape(ev.url)}</link>\n"
            f"      <guid isPermaLink=\"false\">{ev.eid}</guid>\n"
            f"      <pubDate>{format_datetime(ev.start or now)}</pubDate>\n"
            f"      <category>{html.escape(ev.region)}</category>\n"
            f"      <description>{html.escape(body)}</description>\n"
            "    </item>"
        )

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n  <channel>\n'
        "    <title>Event Radar</title>\n"
        "    <link>https://example.invalid/</link>\n"
        "    <description>Hardware, AI/ML, BCI and founder events in Delhi NCR, "
        "India, and globally.</description>\n"
        f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n"
        + "\n".join(items)
        + "\n  </channel>\n</rss>\n"
    )
    path = out_dir / "feed.xml"
    path.write_text(feed, encoding="utf-8")
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Event Radar</title>
<style>
  :root {
    --bg: #fbfbfa; --panel: #ffffff; --ink: #1a1a18; --muted: #6b6b64;
    --line: #e6e5e0; --accent: #7c4dff; --accent-soft: #f0eaff;
    --free: #0f7a52; --free-soft: #e4f4ec; --new: #b25000; --new-soft: #fdeee0;
    --bar: #ded9f5;
    --radius: 14px;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #121214; --panel: #1b1b1f; --ink: #ececea; --muted: #9a9a94;
      --line: #2c2c32; --accent: #a98bff; --accent-soft: #2a2340;
      --free: #4bd39b; --free-soft: #16332a; --new: #ffab5e; --new-soft: #3a2a17;
      --bar: #3a3260;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: var(--sans); line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 32px 20px 80px; }

  header h1 { font-size: 1.6rem; margin: 0 0 4px; letter-spacing: -0.02em; }
  header p { margin: 0; color: var(--muted); font-size: 0.9rem; }

  .controls {
    position: sticky; top: 0; z-index: 5; background: var(--bg);
    padding: 16px 0 12px; margin: 20px 0 8px;
    border-bottom: 1px solid var(--line);
  }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .row + .row { margin-top: 10px; }
  input[type=search] {
    flex: 1 1 240px; min-width: 0; padding: 9px 12px; font: inherit;
    color: var(--ink); background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px;
  }
  input[type=search]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .chip {
    font: inherit; font-size: 0.82rem; padding: 6px 12px; cursor: pointer;
    background: var(--panel); color: var(--muted);
    border: 1px solid var(--line); border-radius: 999px;
    transition: background .12s, color .12s, border-color .12s;
  }
  .chip:hover { color: var(--ink); }
  .chip[aria-pressed="true"] {
    background: var(--accent-soft); color: var(--accent);
    border-color: var(--accent); font-weight: 600;
  }
  .count { margin-left: auto; font-size: 0.8rem; color: var(--muted); font-variant-numeric: tabular-nums; }

  section { margin-top: 36px; }
  .sec-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
  .sec-head h2 { font-size: 1.05rem; margin: 0; letter-spacing: -0.01em; }
  .sec-head span { font-size: 0.82rem; color: var(--muted); }

  .card {
    display: grid; grid-template-columns: 60px 1fr auto; gap: 14px;
    align-items: start; padding: 14px;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: var(--radius); margin-bottom: 8px;
    text-decoration: none; color: inherit;
    transition: border-color .12s, transform .12s;
  }
  .card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .date {
    text-align: center; font-family: var(--mono); font-size: 0.72rem;
    color: var(--muted); padding-top: 2px; line-height: 1.35;
  }
  .date b { display: block; font-size: 1.15rem; color: var(--ink); font-weight: 650; }
  .body h3 { margin: 0 0 3px; font-size: 0.98rem; font-weight: 600; letter-spacing: -0.01em; }
  .meta { font-size: 0.8rem; color: var(--muted); }
  .desc {
    font-size: 0.82rem; color: var(--muted); margin-top: 5px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .badges { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 7px; }
  .badge {
    font-size: 0.68rem; padding: 2px 7px; border-radius: 5px;
    background: var(--accent-soft); color: var(--accent); font-weight: 600;
    letter-spacing: 0.01em;
  }
  .badge.free { background: var(--free-soft); color: var(--free); }
  .badge.new { background: var(--new-soft); color: var(--new); }
  .badge.plain { background: transparent; color: var(--muted); border: 1px solid var(--line); font-weight: 500; }

  .score { text-align: right; min-width: 62px; }
  .bars { display: flex; gap: 2px; justify-content: flex-end; height: 22px; align-items: flex-end; }
  .bars i { width: 4px; background: var(--bar); border-radius: 1px; display: block; }
  .bars i.on { background: var(--accent); }
  .score small { display: block; font-family: var(--mono); font-size: 0.68rem; color: var(--muted); margin-top: 3px; }

  .empty {
    padding: 22px; text-align: center; color: var(--muted); font-size: 0.87rem;
    border: 1px dashed var(--line); border-radius: var(--radius);
  }

  .watch { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 8px; }
  .watch a {
    padding: 11px 13px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; text-decoration: none; color: inherit;
  }
  .watch a:hover { border-color: var(--accent); }
  .watch b { display: block; font-size: 0.88rem; font-weight: 600; }
  .watch span { font-size: 0.76rem; color: var(--muted); }

  footer {
    margin-top: 48px; padding-top: 18px; border-top: 1px solid var(--line);
    font-size: 0.78rem; color: var(--muted);
  }
  footer code { font-family: var(--mono); font-size: 0.75rem; }
  footer a { color: var(--accent); }
  @media (max-width: 560px) {
    .card { grid-template-columns: 48px 1fr; }
    .score { grid-column: 2; text-align: left; }
    .bars { justify-content: flex-start; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Event Radar</h1>
    <p id="sub"></p>
  </header>

  <div class="controls">
    <div class="row">
      <input type="search" id="q" placeholder="Search titles, venues, topics…" autocomplete="off">
      <button class="chip" id="f-free" aria-pressed="false">Free only</button>
      <button class="chip" id="f-person" aria-pressed="false">In person</button>
      <button class="chip" id="f-new" aria-pressed="false">New</button>
      <span class="count" id="count"></span>
    </div>
    <div class="row" id="tags"></div>
  </div>

  <div id="list"></div>

  <section id="watch-sec">
    <div class="sec-head"><h2>Global watchlist</h2><span>Curated by hand in config.yaml</span></div>
    <div class="watch" id="watch"></div>
  </section>

  <footer id="foot"></footer>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const state = { q: '', free: false, person: false, fresh: false, tags: new Set() };

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const allEvents = DATA.sections.flatMap(s => s.events);
document.getElementById('sub').textContent =
  `${allEvents.length} events in the next ${DATA.horizon} days · updated ${DATA.generated}`;

// Tag chips, most common first.
const tagCounts = {};
allEvents.forEach(e => (e.tags || []).forEach(t => { tagCounts[t] = (tagCounts[t] || 0) + 1; }));
const tagBar = document.getElementById('tags');
Object.entries(tagCounts).sort((a, b) => b[1] - a[1]).forEach(([tag, n]) => {
  const b = document.createElement('button');
  b.className = 'chip';
  b.setAttribute('aria-pressed', 'false');
  b.textContent = `${tag} ${n}`;
  b.onclick = () => {
    const on = b.getAttribute('aria-pressed') === 'true';
    b.setAttribute('aria-pressed', String(!on));
    on ? state.tags.delete(tag) : state.tags.add(tag);
    render();
  };
  tagBar.appendChild(b);
});

function toggle(id, key) {
  const el = document.getElementById(id);
  el.onclick = () => {
    state[key] = !state[key];
    el.setAttribute('aria-pressed', String(state[key]));
    render();
  };
}
toggle('f-free', 'free');
toggle('f-person', 'person');
toggle('f-new', 'fresh');
document.getElementById('q').oninput = e => { state.q = e.target.value.toLowerCase().trim(); render(); };

function matches(e) {
  if (state.free && !e.isFree) return false;
  if (state.person && e.isOnline) return false;
  if (state.fresh && !e.isNew) return false;
  if (state.tags.size && !(e.tags || []).some(t => state.tags.has(t))) return false;
  if (state.q) {
    const hay = `${e.title} ${e.venue} ${e.city} ${e.description} ${(e.tags||[]).join(' ')}`.toLowerCase();
    if (!state.q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}

const MAX_BARS = 6;
function bars(score) {
  const filled = Math.max(1, Math.min(MAX_BARS, Math.round(score / 4)));
  let out = '';
  for (let i = 0; i < MAX_BARS; i++) {
    out += `<i class="${i < filled ? 'on' : ''}" style="height:${6 + i * 3}px"></i>`;
  }
  return out;
}

function card(e) {
  const [d, m] = e.dayLabel.split(' ');
  const where = [e.venue, e.city].filter(Boolean).join(' · ') || (e.isOnline ? 'Online' : '');
  const badges = [];
  if (e.isNew) badges.push('<span class="badge new">new</span>');
  if (e.isFree) badges.push('<span class="badge free">Free</span>');
  else if (e.price && e.price !== '—') badges.push(`<span class="badge">${esc(e.price)}</span>`);
  if (e.isOnline) badges.push('<span class="badge plain">online</span>');
  (e.tags || []).slice(0, 3).forEach(t => badges.push(`<span class="badge plain">${esc(t)}</span>`));
  const via = e.alsoOn && e.alsoOn.length
    ? `${esc(e.source)} · also on ${e.alsoOn.map(esc).join(', ')}`
    : esc(e.source);

  return `<a class="card" href="${esc(e.url)}" target="_blank" rel="noopener">
    <div class="date"><b>${esc(d)}</b>${esc(m || '')}<br>${esc(e.startLabel.split('· ')[1] || '')}</div>
    <div class="body">
      <h3>${esc(e.title)}</h3>
      <div class="meta">${esc(where)}${where ? ' · ' : ''}${via}</div>
      ${e.description ? `<div class="desc">${esc(e.description)}</div>` : ''}
      <div class="badges">${badges.join('')}</div>
    </div>
    <div class="score"><div class="bars">${bars(e.score)}</div><small>${e.score}</small></div>
  </a>`;
}

function render() {
  let shown = 0;
  const html = DATA.sections.map(sec => {
    const hits = sec.events.filter(matches);
    shown += hits.length;
    const body = hits.length
      ? hits.map(card).join('')
      : `<div class="empty">Nothing here${sec.events.length ? ' matching those filters' : ' in this run'}.</div>`;
    return `<section>
      <div class="sec-head">
        <h2>${esc(sec.title)}</h2>
        <span>${esc(sec.subtitle)} · min score ${sec.threshold ?? '—'} · ${hits.length} shown</span>
      </div>${body}
    </section>`;
  }).join('');
  document.getElementById('list').innerHTML = html;
  document.getElementById('count').textContent = `${shown} / ${allEvents.length}`;
}

document.getElementById('watch').innerHTML = DATA.watchlist.map(w =>
  `<a href="${esc(w.url)}" target="_blank" rel="noopener">
     <b>${esc(w.name)}</b><span>${esc(w.note)}${w.month ? ' · ' + esc(w.month) : ''}</span>
   </a>`).join('');

const s = DATA.stats || {};
document.getElementById('foot').innerHTML = `
  <p>Scanned ${esc(s.sources_ok ?? 0)} of ${esc(s.sources_tried ?? 0)} sources ·
  ${esc(s.raw ?? 0)} listings seen · ${esc(s.after_dedupe ?? 0)} after dedupe ·
  ${esc(s.kept ?? 0)} above threshold · <b>${esc(s.new_count ?? 0)} new</b></p>
  ${s.rejected_line ? `<p>Dropped: ${esc(s.rejected_line)}</p>` : ''}
  ${(s.failed_sources || []).length ? `<p>Quiet sources: <code>${esc((s.failed_sources || []).join(', '))}</code></p>` : ''}
  <p><a href="feed.xml">RSS feed</a> · bars are the relevance score · rebuilt daily at ~07:00 IST</p>`;

render();
</script>
</body>
</html>
"""
