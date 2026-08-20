# Event Radar

Daily scan of public event sites for the things you actually care about —
hardware, AI/ML, deep learning, BCI/medical devices, and founder rooms — split
into Delhi NCR, rest of India, and a curated global watchlist. Output is a
static page plus an RSS feed, rebuilt every morning by GitHub Actions.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m radar.main            # scan + rebuild site/
open site/index.html
.venv/bin/python -m radar.main --no-fetch # rebuild page only, no network
```

A full scan is ~40 page fetches plus one description fetch per newly seen
event, so the first run takes a few minutes and later ones are much quicker.

## Sources

| Source | How it's read | State |
|---|---|---|
| Luma | city page's embedded Next.js state. The `api.lu.ma/discover` endpoints are tried first but currently 404, so the page state is the live path | working |
| Meetup | `meetup.com/find` Apollo state, geo-searched around Gurugram | working, 12 results per search term |
| Eventbrite | `__SERVER_DATA__` blob on city search pages | working |
| AllEvents | schema.org JSON-LD | working, per-city category paths vary |
| Devfolio | its own `api.devfolio.co/api/search/hackathons` — the HTML renders client-side and carries nothing | working |
| Townscript, Commudle | JS-rendered, no server-side event data and no JSON-LD | **disabled** — fetchers kept in `sources.py` for the day that changes |
| Global conferences | curated list in `config.yaml` — dates move yearly, scraping them is churn for no gain | by hand |

No API keys anywhere. Everything used is public HTML or a public JSON endpoint
the site's own frontend calls.

Two things worth knowing about coverage:

- **Meetup returns 12 events per search term and pads with weak matches.** The
  keyword *is* applied server-side, but its idea of relevance is loose, so a
  chunk of what it returns is filtered out again by scoring.
- **Luma's city page ships titles with no descriptions.** Scoring a title alone
  can't tell a neurotech workshop from a run club, so after dedupe each new
  event's page is fetched once for its meta description. Results are cached in
  the database, so the cost is a burst on the first run and near-nothing daily.

## No duplicates

Three layers, in order:

1. **Canonical URL** — tracking params and `www.` stripped, so the same link
   posted twice is one row.
2. **Fuzzy title match** — titles are normalised (issue numbers, years, months,
   "edition", "meetup", "free", "tickets" all stripped), then compared within
   the *same day and same metro*. NCR is one metro, so an event tagged Gurugram
   on Luma and Delhi on Meetup still collapses. Threshold is 0.82 similarity —
   tuned so "AI Meetup Vol. 3" and "Vol. 4" merge (they can't share a day
   anyway) while "PyTorch Workshop" and "TensorFlow Workshop" stay separate.
3. **Stable ID across runs** — the row key uses the same normalised title, so
   yesterday's event matches today's scan even if a different source wins.

When duplicates merge, the better listing survives (Luma/Meetup > Eventbrite >
Townscript > AllEvents), missing fields are backfilled from the loser, and the
page shows *also on Meetup, AllEvents*.

## Only credible sources

Every event must link to a domain on the allowlist in `radar/trust.py`.
Anything else is dropped outright rather than shown with a warning.

- **Tier 1** (Luma, Meetup, Eventbrite, Devfolio, Commudle, KonfHub, Unstop,
  HackerEarth, IEEE, ACM, NASSCOM, TiE, Startup India) and any `.ac.in`,
  `.edu`, `.gov.in`, `.res.in` host — institutional listings are credible by
  construction, which is how IIT Delhi and IEEE workshops get through.
- **Tier 2** (AllEvents, Townscript, Insider, 10Times, BookMyShow) — real
  platforms, looser moderation, so they must have a clean listing to survive.

A good host isn't enough on its own — anyone can post to Eventbrite. Each
listing is also checked for spam wording ("100% job guarantee", "whatsapp us",
"earn 2 lakh"), all-caps titles, missing dates, and stub listings with no
venue, price, or real description. Cross-posting counts as corroboration: an
event on two platforms gets a trust bump.

The run log names every rejection and why, e.g.
`3 untrusted (spam wording×1; sparse listing×1; unverified (random-blog.xyz)×1)`.
The page footer carries the same counts. To trust a new source, add its domain
to `TIER1`/`TIER2` — if it isn't listed, its events never appear.

## Tuning what shows up

Everything lives in `config.yaml`.

- **`keywords`** — four weighted buckets. A term match adds its bucket weight,
  capped at three matches per bucket so keyword-stuffed listings can't game it.
  Free adds +1, online subtracts 2 (rooms are the point).
- **`search_terms`** — what gets typed into the search box on sites that need a
  query. Each term is a separate page fetch, so keep the list short.
- **`thresholds`** — the score an event must clear. Local is lenient (4),
  India needs to justify a flight (7), global is 12. Too much noise in a
  section, raise its number by 2 and rerun with `--no-fetch` to see the effect.
- **`blocklist`** — instant drop, no matter the score.
- **`curated_events`** — hand-verified events with real dates. They flow through
  scoring and regions like scraped ones and appear in the NCR/India sections
  once they fall inside the digest window, so entries dated further out simply
  surface later. This is where the deep-tech conferences live: the sites that
  list them either block automated requests (10times, embs.org) or ship no
  machine-readable data (KonfHub, IIT Delhi, NASSCOM), and their dates are
  announced a year ahead and barely move. Each entry needs `name`, `url` and
  `start`; `topic` is what the scorer reads, since a conference title is
  usually just a proper noun. Worth a check each January.
- **`global_watchlist`** — recurring conferences with no announced date yet.
  Rendered as plain cards. Edit by hand.

The bars beside each event are its relevance score. Skim the bars, not the list.

Two scoring rules live in code rather than config, because both exist to kill a
specific class of false positive:

- **Title matches count double what description-only matches do.** An event
  that's actually about hardware says so in its title. "India Furniture
  Conclave" only mentions hardware deep in its blurb — furniture hardware — and
  a "Theatre Workshop" matched `speech` because its description talks about
  public speaking. Weighting by position drops both without needing a
  hand-maintained list of exceptions.
- **Format words can't qualify an event on their own.** "Workshop",
  "conference", "summit", "meetup" and friends say nothing about subject, so at
  least one topical term has to match somewhere.

## Deploy

1. Push to a **public** GitHub repo (public = unlimited Actions minutes and
   free Pages; private repos need a paid plan for Pages).
2. Settings → Pages → Source: **GitHub Actions**.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Actions tab → Event Radar → **Run workflow** to trigger the first scan,
   then it runs itself at ~07:00 IST daily.

The workflow commits `events.db` back to the repo after each run. That keeps
the "new since last run" flags accurate and counts as repo activity, so GitHub
won't auto-disable the schedule after 60 idle days.

Optional email digest of new events only: add repo secrets `SMTP_USER`,
`SMTP_PASS` (Gmail app password), `DIGEST_TO`.

## When a source goes quiet

Scrapers break when sites change markup — that's expected, and each source
fails independently so the run still completes. The Actions log names the
failing fetcher and the error. Luma and Meetup depend on embedded JSON; the
JSON-LD sources are far more stable. Worst case, delete a source from
`collect()` in `radar/main.py` and the rest keeps working.

## Files

```
config.yaml              filters, cities, thresholds, watchlist
requirements.txt         requests, PyYAML, python-dateutil, beautifulsoup4
radar/model.py           the Event shape every source normalises into
radar/sources.py         one function per site + the description enrichment pass
radar/trust.py           domain allowlist + junk-listing detection
radar/dedupe.py          URL canonicalisation + fuzzy cross-source merge
radar/score.py           relevance + region
radar/db.py              SQLite, stable IDs across runs
radar/render.py          static page + RSS
radar/main.py            orchestration, email digest
.github/workflows/       daily run + Pages deploy
```
