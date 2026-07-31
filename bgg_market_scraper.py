#!/usr/bin/env python3
"""
bgg_market_scraper.py

Scrapes BGG GeekMarket listings for a given country and enriches them with
game stats (rating, rank, player counts) from BGG's official XML API2.

Run this on your own machine (needs real internet access + a browser).

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python bgg_market_scraper.py --country GB --max-pages 20 --out bgg_market_data.json

Game stats (rating, players, weight, etc.) are cached in a local SQLite
database (bgg_cache.db by default) so re-running the scraper only needs to
call the API for games it hasn't seen recently — repeat runs are much
faster once the cache is warm. Use --refresh-days 0 to force a full
refresh, or --cache-db to point at a different cache file.

Notes:
- The GeekMarket browse page (https://boardgamegeek.com/market?...) is a
  JS-rendered app. Critically, the rendered HTML links each listing to
  /market/product/{listingid} — BGG's own internal listing ID — NOT to
  /boardgame/{id}. The real BGG game ID only exists in the JSON the page
  fetches from BGG's API (confirmed live: a top-level "objectid" field on
  each product), so this script intercepts that network response via
  Playwright rather than scraping visible DOM text/links. Run with
  --dump-json to inspect bgg_api_sample.json if BGG changes this shape.
- BGG's site sits behind Cloudflare bot protection. The XML API2 calls in
  fetch_game_stats() go through the same Playwright browser context used to
  load the marketplace pages (context.request), not a bare `requests` call,
  since a sessionless request was getting blocked with a 401.
- The XML API2 is official/documented and much more reliable:
  https://boardgamegeek.com/wiki/page/BGG_XML_API2
  Be a good citizen: batch requests (comma-separated ids) and add small
  delays between calls.
"""

import argparse
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

MARKET_URL = "https://boardgamegeek.com/market"
XMLAPI_THING = "https://boardgamegeek.com/xmlapi2/thing"

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    thing_id TEXT PRIMARY KEY,
    name TEXT,
    yearpublished TEXT,
    minplayers TEXT,
    maxplayers TEXT,
    best_with TEXT,
    playingtime TEXT,
    average_rating REAL,
    bayes_rating REAL,
    num_ratings INTEGER,
    rank INTEGER,
    weight REAL,
    last_updated TEXT
)
"""

HEADERS = {"User-Agent": "Mozilla/5.0 (personal research script; contact: your-email@example.com)"}


def scrape_market_pages(context, country="GB", sort="lowprice", max_pages=20, debug=False, dump_json=False):
    """Render each marketplace page and capture the JSON response Angular
    uses to populate the listings, rather than scraping the rendered DOM.

    Why: the rendered HTML links each listing to /market/product/{listingid}
    (BGG's internal marketplace listing ID) — the actual BGG game ID is not
    present anywhere in the visible HTML/attributes. It only exists in the
    JSON payload the page fetches from BGG's API and hands to Angular. So we
    intercept that response instead of trying to parse text out of the DOM.

    Confirmed from a live sample: each product dict has a top-level
    "objectid" field (e.g. "194562") — no need to dig into objectlink.
    """
    listings = []
    page = context.new_page()

    for pageid in range(1, max_pages + 1):
        url = f"{MARKET_URL}?pageid={pageid}&country={country}&sort={sort}&displaymode=list"
        print(f"[fetch] {url}")

        captured = []

        def handle_response(response, _captured=captured):
            try:
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                data = response.json()
                _captured.append({"url": response.url, "data": data})
            except Exception:
                pass

        page.on("response", handle_response)
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)
        page.remove_listener("response", handle_response)

        # Find the captured response that actually holds the product list.
        products = None
        for item in captured:
            data = item["data"]
            found = _find_products_list(data)
            if found:
                products = found
                if dump_json:
                    Path("bgg_api_sample.json").write_text(json.dumps(item, indent=2)[:20000])
                    print(f"  wrote sample API response to bgg_api_sample.json (from {item['url']})")
                break

        if not products:
            print(f"  no product data found in API responses on page {pageid} — stopping")
            if debug:
                Path(f"debug_page_{pageid}.html").write_text(page.content())
                urls = [c["url"] for c in captured]
                Path(f"debug_page_{pageid}_responses.json").write_text(json.dumps(urls, indent=2))
            break

        page_listings = []
        for product in products:
            if not product or not product.get("productid"):
                continue
            objectlink = product.get("objectlink") or {}
            thing_id = product.get("objectid") or objectlink.get("id")
            if not thing_id:
                continue  # couldn't resolve a game id for this listing
            thing_id = str(thing_id)

            page_listings.append({
                "thing_id": thing_id,
                "name": objectlink.get("name"),
                "price_raw": _format_price(product),
                "condition": product.get("prettycondition") or product.get("condition"),
                "seller": (product.get("linkeduser") or {}).get("username"),
                "listing_url": f"https://boardgamegeek.com/market/product/{product['productid']}",
            })

        print(f"  found {len(page_listings)} listings with resolved game ids "
              f"(of {len(products)} total on page)")
        if page_listings:
            listings.extend(page_listings)
        else:
            print("  no game ids resolved — check bgg_api_sample.json (--dump-json)")
            break

    page.close()
    return listings


def _find_products_list(data, _depth=0):
    """Recursively search a JSON blob for a list of dicts that look like
    marketplace product entries (have 'price' and 'objectlink' keys)."""
    if _depth > 6:
        return None
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "price" in data[0] and "objectlink" in data[0]:
            return data
        for item in data:
            found = _find_products_list(item, _depth + 1)
            if found:
                return found
    elif isinstance(data, dict):
        for value in data.values():
            found = _find_products_list(value, _depth + 1)
            if found:
                return found
    return None


def _format_price(product):
    price = product.get("price")
    currency = product.get("currency", "")
    if price is None:
        return None
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, currency + " " if currency else "")
    return f"{symbol}{price}"


def _init_cache(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(CACHE_SCHEMA)
    conn.commit()
    return conn


def _cache_get_fresh(conn, ids, refresh_days):
    """Return {thing_id: row_dict} for cached rows still within refresh_days."""
    if not ids:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=refresh_days)).isoformat()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM games WHERE thing_id IN ({placeholders}) AND last_updated >= ?",
        [*ids, cutoff],
    ).fetchall()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(games)")]
    return {dict(zip(cols, row))["thing_id"]: dict(zip(cols, row)) for row in rows}


def _cache_upsert(conn, tid, row):
    row = {**row, "thing_id": tid, "last_updated": datetime.now(timezone.utc).isoformat()}
    cols = list(row.keys())
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "thing_id")
    conn.execute(
        f"INSERT INTO games ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(thing_id) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def fetch_game_stats(context, thing_ids, batch_size=20, delay=1.5, cache_db="bgg_cache.db", refresh_days=7):
    """Query XML API2 in batches, using a local SQLite cache to skip games
    already fetched within `refresh_days`. Returns dict keyed by thing_id.
    Pass refresh_days=0 to force re-fetching everything.

    Uses the Playwright browser context's request API (context.request)
    rather than the `requests` library: BGG's site sits behind Cloudflare
    bot protection, and a bare `requests` call with no browser session was
    getting blocked (401, then a non-XML error body). Routing through the
    same context that already loaded the marketplace pages carries whatever
    cookies let that succeed.
    """
    ids = list(dict.fromkeys(thing_ids))  # dedupe, keep order
    conn = _init_cache(cache_db)

    cached = _cache_get_fresh(conn, ids, refresh_days) if refresh_days > 0 else {}
    to_fetch = [tid for tid in ids if tid not in cached]
    print(f"[cache] {len(cached)} games served from cache, {len(to_fetch)} need fetching")

    stats = {tid: {k: v for k, v in row.items() if k not in ("thing_id", "last_updated")}
             for tid, row in cached.items()}

    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        params = {"id": ",".join(batch), "stats": "1"}
        print(f"[api] fetching stats for {len(batch)} games ({i + len(batch)}/{len(to_fetch)})")

        resp = context.request.get(XMLAPI_THING, params=params, timeout=30000)
        if resp.status != 200:
            print(f"  warning: got status {resp.status}, retrying once after delay")
            time.sleep(3)
            resp = context.request.get(XMLAPI_THING, params=params, timeout=30000)

        body = resp.text()
        if resp.status != 200 or not body.strip().startswith("<"):
            print(f"  giving up on this batch — status {resp.status}, body did not look like XML "
                  f"(first 200 chars): {body[:200]!r}")
            continue

        root = ET.fromstring(body)
        for item in root.findall("item"):
            tid = item.get("id")
            name_el = item.find("name[@type='primary']")
            name = name_el.get("value") if name_el is not None else None

            minplayers = _attr(item, "minplayers")
            maxplayers = _attr(item, "maxplayers")
            playingtime = _attr(item, "playingtime")
            yearpublished = _attr(item, "yearpublished")

            ratings = item.find("statistics/ratings")
            average = _attr(ratings, "average") if ratings is not None else None
            bayesaverage = _attr(ratings, "bayesaverage") if ratings is not None else None
            usersrated = _attr(ratings, "usersrated") if ratings is not None else None
            weight = None
            if ratings is not None:
                weight_el = ratings.find("averageweight")
                if weight_el is not None:
                    weight = weight_el.get("value")

            rank = None
            if ratings is not None:
                rank_el = ratings.find("ranks/rank[@name='boardgame']")
                if rank_el is not None:
                    rank_val = rank_el.get("value")
                    rank = None if rank_val == "Not Ranked" else rank_val

            best_with = _best_player_count(item)

            row = {
                "name": name,
                "yearpublished": yearpublished,
                "minplayers": minplayers,
                "maxplayers": maxplayers,
                "best_with": best_with,
                "playingtime": playingtime,
                "average_rating": float(average) if average else None,
                "bayes_rating": float(bayesaverage) if bayesaverage else None,
                "num_ratings": int(usersrated) if usersrated else None,
                "rank": int(rank) if rank else None,
                "weight": float(weight) if weight else None,
            }
            stats[tid] = row
            _cache_upsert(conn, tid, row)

        conn.commit()
        time.sleep(delay)

    conn.close()
    return stats


def _attr(el, tag):
    if el is None:
        return None
    child = el.find(tag)
    return child.get("value") if child is not None else None


def _best_player_count(item):
    """Parse the 'suggested_numplayers' community poll for the count with the most 'Best' votes."""
    poll = item.find("poll[@name='suggested_numplayers']")
    if poll is None:
        return None
    best_count, best_votes = None, -1
    for result in poll.findall("results"):
        numplayers = result.get("numplayers")
        for r in result.findall("result"):
            if r.get("value") == "Best":
                votes = int(r.get("numvotes", 0))
                if votes > best_votes:
                    best_votes = votes
                    best_count = numplayers
    return best_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="GB")
    ap.add_argument("--sort", default="lowprice")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--out", default="bgg_market_data.json")
    ap.add_argument("--cache-db", default="bgg_cache.db", help="SQLite file caching game stats between runs")
    ap.add_argument("--refresh-days", type=int, default=7,
                    help="Reuse cached game stats younger than this many days; 0 forces a full refresh")
    ap.add_argument("--debug", action="store_true", help="run browser headed + dump HTML if nothing found")
    ap.add_argument("--dump-json", action="store_true",
                    help="save the first captured API response to bgg_api_sample.json for field-name verification")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.debug)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])

        listings = scrape_market_pages(context, args.country, args.sort, args.max_pages, args.debug, args.dump_json)
        print(f"\nTotal listings scraped: {len(listings)}")

        if not listings:
            print("No listings scraped. Run again with --dump-json and check bgg_api_sample.json to see the raw "
                  "API response shape.")
            browser.close()
            return

        thing_ids = [l["thing_id"] for l in listings]
        stats = fetch_game_stats(context, thing_ids, cache_db=args.cache_db, refresh_days=args.refresh_days)

        browser.close()

    merged = []
    for l in listings:
        s = stats.get(l["thing_id"], {})
        merged.append({**l, **s})

    Path(args.out).write_text(json.dumps(merged, indent=2))
    print(f"\nWrote {len(merged)} rows to {args.out}")
    print(f"Game stats cached in {args.cache_db} — future runs will reuse them for {args.refresh_days} days.")
    print("Now either:")
    print("  - open bgg_market_viewer.html in your browser and load this file, or")
    print(f"  - run: streamlit run bgg_market_app.py -- --data {args.out}")


if __name__ == "__main__":
    main()
