# BGG Marketplace Tools

Two small, related tools for finding good board game deals on the BGG
GeekMarket, built around the fact that BGG's marketplace browse page is a
JS-rendered app with no public API of its own — both tools intercept the
same private JSON endpoint the browse page itself uses, rather than
scraping visible HTML.

1. **Market Explorer** — scrape all UK GeekMarket listings, enrich them
   with BGG ratings/weight/player counts, then sort and filter to find
   good games. Files: `bgg_market_scraper.py`, `bgg_market_viewer.html`,
   `bgg_market_app.py`.
2. **Wishlist Price Finder** — pull your BGG wishlist and find the
   cheapest current UK listing for each game on it. Files:
   `bgg_wishlist_prices.py`, `bgg_wishlist_viewer.html`.

Everything runs locally on your own machine — nothing is hosted, no data
leaves your computer except requests to boardgamegeek.com itself.

---

## Setup (shared by both tools)

```bash
pip install playwright pandas streamlit   # streamlit/pandas only needed for bgg_market_app.py
playwright install chromium
```

Both tools use Playwright to load real pages in a headless Chromium browser
(not just plain HTTP requests) — this is necessary because BGG's site is a
JS-rendered app and sits behind Cloudflare bot protection, which a bare
HTTP client would get blocked by.

---

## Tool 1: Market Explorer

Scrapes every GeekMarket listing shipping from a given country, matches
each one to its BGG game ID, and enriches it with rating/rank/weight/player
count data — so you can find "highly-rated games for sale that play well
at 4 players, under £30" instead of paging through the marketplace by hand.

### `bgg_market_scraper.py`

The data-gathering script. Run this first.

```bash
python bgg_market_scraper.py --country GB --max-pages 20 --api-token YOUR_TOKEN --out bgg_market_data.json
```

**What it does:**
1. Loads each page of `boardgamegeek.com/market?...` in a real browser and
   intercepts the JSON response the page itself uses to render listings
   (`api.geekdo.com/api/market/products`) — this is where each listing's
   real BGG game ID lives; it's not present in the visible HTML, which only
   links to `/market/product/{listingid}` (BGG's internal listing ID, not
   the game's ID).
2. Collects every unique game ID found, then queries BGG's official
   **XML API2** (`xmlapi2/thing`) in batches of 20 for each game's average
   rating, Bayesian rating, rank, number of ratings, min/max players,
   "best with N players" community poll result, playing time, and
   complexity ("weight").
3. Caches those game stats in a local SQLite database so re-running the
   scraper later only needs to look up games it hasn't seen recently —
   much faster refreshes once the cache is warm.
4. Merges listings + stats and writes one JSON file.

**Requires a BGG API token.** Since July 2025, BGG requires a registered
application token (`Authorization: Bearer <token>`) for XML API2 calls that
aren't downloading your own collection — register at
https://boardgamegeek.com/using_the_xml_api (can take a week or more to be
approved). Pass it via `--api-token` or set it once as an environment
variable:
```bash
export BGG_API_TOKEN=your_token_here
```
Without a token, listings will still scrape fine, but every rating/stats
lookup will fail with a 401 and that batch will be skipped.

**CLI options:**

| Flag | Default | Meaning |
|---|---|---|
| `--country` | `GB` | Two-letter country code to filter listings by |
| `--sort` | `lowprice` | Marketplace sort order (`lowprice`, `price`, `recent`) |
| `--max-pages` | `20` | How many marketplace pages to scrape (~50 listings/page) |
| `--out` | `bgg_market_data.json` | Output file for the merged data |
| `--api-token` | *(from `BGG_API_TOKEN` env var)* | Your registered BGG API token |
| `--cache-db` | `bgg_cache.db` | SQLite file caching game stats between runs |
| `--refresh-days` | `7` | Reuse cached stats younger than this; `0` forces a full refresh |
| `--debug` | off | Run browser visibly, dump extra debug files if nothing is found |
| `--dump-json` | off | Save one raw API response to `bgg_api_sample.json` (useful if BGG changes its data format and things stop working — lets you see the new shape and adjust) |

Be a considerate scraper: `--max-pages` and the cache both exist to avoid
hammering BGG's servers more than necessary.

### `bgg_market_viewer.html`

A standalone page — no server, no build step, just open it in a browser.
Click "Load" and pick the JSON file the scraper produced. Gives you a
sortable table (click any column header) and filters for:
- Minimum rating
- Maximum weight (complexity)
- Minimum number of ratings (to exclude games with 1–2 ratings skewing the average)
- Maximum price
- Player count — either "supports N players" or "best at exactly N players" (toggle)

Everything runs client-side in your browser; the JSON file never leaves
your machine.

### `bgg_market_app.py`

An alternative to the HTML viewer, built with Streamlit instead — same
filters, but as native widgets (sliders, dropdowns) instead of plain HTML
inputs, and results shown in a sortable data table.

```bash
streamlit run bgg_market_app.py -- --data bgg_market_data.json
```
(the `--` is required so Streamlit passes `--data` through to the script
instead of trying to parse it itself)

Pick whichever viewer suits you — the HTML file needs nothing installed;
the Streamlit app needs Python running but has nicer input widgets.

---

## Tool 2: Wishlist Price Finder

A more targeted tool: instead of browsing the whole marketplace, it pulls
*your* BGG wishlist and checks current UK listings for just those specific
games — good for "which of the games I actually want am I most likely to
find cheap right now?"

### `bgg_wishlist_prices.py`

```bash
python bgg_wishlist_prices.py --username YOUR_BGG_USERNAME --out bgg_wishlist_prices.json
```

**Login, without ever handling your password:** BGG's login page sits
behind Cloudflare Turnstile, which is specifically designed to block
scripted form-filling — so this script doesn't try to automate your
credentials at all. On first run, it opens a real, visible browser window
to the BGG login page; you log in there yourself, the same as normal. The
script detects the moment you're logged in (by watching for BGG's session
cookies) and saves that session to `bgg_auth.json` for reuse — future runs
skip the login window entirely unless the saved session has expired.

**No API token needed for this one.** BGG's own policy exempts downloading
*your own collection* while logged in from the application-registration
requirement — so the wishlist pull uses that authenticated browser session
directly against the XML API2 collection endpoint, no token required. The
per-game marketplace price lookups use the same public listings endpoint
`bgg_market_scraper.py` uses, which also needs no token.

**What it does:**
1. Logs in (or reuses the saved session).
2. Fetches your wishlist via `xmlapi2/collection?wishlist=1`, including
   each game's average/Bayesian rating (bundled into the same response as
   a bonus — collection-endpoint stats don't include player count or
   weight, though; that data only comes from the `thing` endpoint, which
   does need the registered token — see note below).
3. For each wishlisted game, loads the GB marketplace filtered to that one
   game and finds the cheapest current listing, its seller, condition, and
   how many total copies are listed.
4. Writes one JSON file with all of it.

This makes one page load per wishlist game, so a wishlist of 100 games
takes a few minutes — there's a small delay between lookups by design, to
stay polite to BGG's servers.

**CLI options:**

| Flag | Default | Meaning |
|---|---|---|
| `--username` | *(required)* | Your BGG username |
| `--country` | `GB` | Country to check listings for |
| `--out` | `bgg_wishlist_prices.json` | Output file |
| `--auth-state` | `bgg_auth.json` | Where the saved login session is stored |
| `--delay` | `1.0` | Seconds to wait between per-game marketplace lookups |

**Known limitation:** ratings shown here come from the collection
endpoint, which doesn't include player count or weight — only
`bgg_market_scraper.py`'s use of the `thing` endpoint (which needs the
registered token) has those. If you get a token, the two datasets could be
merged for a fuller wishlist view — ask if you want that wired in.

### `bgg_wishlist_viewer.html`

Same idea as the market viewer: open it, load
`bgg_wishlist_prices.json`, get a sortable table. Filters for:
- Maximum price
- Only show games that currently have a UK listing at all
- Minimum wishlist priority (BGG's 1–5 "must have" → "like it" scale)

Click "Best UK Price" to sort cheapest-first.

---

## A note on how these were built

BGG's marketplace browse page and login page are both modern JS apps with
real bot-protection (Cloudflare Turnstile) in front of them — that shaped
several design choices here: intercepting network responses instead of
parsing rendered HTML (the game ID isn't in the HTML at all), and having
*you* do the actual login rather than the script. Both tools also respect
BGG's stated API policy: the registered-token requirement for arbitrary
game lookups, and the collection-endpoint exemption for your own data.
