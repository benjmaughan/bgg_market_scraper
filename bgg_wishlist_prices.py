#!/usr/bin/env python3
"""
bgg_wishlist_prices.py

Finds the cheapest current UK GeekMarket listing for every game on your
BGG wishlist.

Also cross-references BoardGamePrices.co.uk (a UK-focused price comparison
site covering ~280 online stores) for each game's cheapest price and stock
status, using their public documented API — no scraping needed there.
Per their API terms: results are cached locally for at least an hour, and
the viewer links back to boardgameprices.co.uk for each game.

Caching: your wishlist snapshot, the per-game GeekMarket price lookups, and
the BoardGamePrices data are all cached locally in bgg_wishlist_cache.db /
bgp_cache.db. Re-running the script within the refresh window reuses cached
data instead of re-fetching — and if EVERYTHING is still fresh, it skips
opening a browser/logging into BGG at all. Use --force-refresh to bypass
all caches, or the individual --*-refresh-hours flags for finer control.

Why this doesn't need a registered API token:
- BGG's collection endpoint (xmlapi2/collection) is exempt from the "must
  register an application" rule *when downloading your own collection while
  logged in* (see https://boardgamegeek.com/using_the_xml_api). So this
  script logs you into a real browser session first, then uses that.
- The per-game marketplace price lookup uses the same private API the
  GeekMarket browse page itself uses (api.geekdo.com/api/market/products),
  which is what bgg_market_scraper.py already relies on — no login or token
  needed for that part, since it's what powers the public browse page.
- BoardGamePrices.co.uk's API is public and documented at
  https://boardgameprices.co.uk/api/plugin — no auth needed, just their
  requested attribution and caching courtesy.

Login flow:
- BGG's login page sits behind Cloudflare Turnstile, which is specifically
  designed to block scripted form-filling. So instead of automating your
  password entry, this script opens a REAL browser window, you log in
  yourself (once), and it detects the logged-in session and saves it to
  bgg_auth.json for reuse on future runs — no password ever touches this
  script.

Setup:
    pip install playwright requests
    playwright install chromium

Usage:
    python bgg_wishlist_prices.py --username YOUR_BGG_USERNAME --out bgg_wishlist_prices.json

    # To view results on your phone via Google Drive/iCloud Drive/Dropbox
    # instead of connecting to this machine over the network, add:
    python bgg_wishlist_prices.py --username YOUR_BGG_USERNAME \\
        --sync-dir "~/Google Drive/My Drive/bgg"
"""

import argparse
import json
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

COLLECTION_URL = "https://boardgamegeek.com/xmlapi2/collection"
MARKET_API_URL = "https://boardgamegeek.com/market"
BGP_API_URL = "https://boardgameprices.co.uk/api/info"
AUTH_STATE_FILE = "bgg_auth.json"
LOGIN_COOKIE_NAMES = ("bggusername",)  # SessionID is set for anonymous visits too — not a reliable signal
WISHLIST_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wishlist_snapshot (
    username TEXT PRIMARY KEY,
    data TEXT,
    last_updated TEXT
);
CREATE TABLE IF NOT EXISTS market_listings (
    thing_id TEXT,
    country TEXT,
    data TEXT,
    last_updated TEXT,
    PRIMARY KEY (thing_id, country)
);
"""

HEADERS = {"User-Agent": "Mozilla/5.0 (personal script; BGG wishlist price checker)"}


def get_cached_wishlist(conn, username, refresh_hours):
    """Return a cached wishlist snapshot if it's within refresh_hours, else None."""
    if refresh_hours <= 0:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=refresh_hours)).isoformat()
    row = conn.execute(
        "SELECT data FROM wishlist_snapshot WHERE username = ? AND last_updated >= ?",
        (username, cutoff),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_wishlist_cache(conn, username, items):
    conn.execute(
        "INSERT INTO wishlist_snapshot (username, data, last_updated) VALUES (?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET data=excluded.data, last_updated=excluded.last_updated",
        (username, json.dumps(items), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_cached_market_listings(conn, thing_ids, country, refresh_hours):
    """Return {thing_id: (listing, count)} for whichever ids have a fresh cache entry."""
    if refresh_hours <= 0 or not thing_ids:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=refresh_hours)).isoformat()
    placeholders = ",".join("?" * len(thing_ids))
    rows = conn.execute(
        f"SELECT thing_id, data FROM market_listings "
        f"WHERE thing_id IN ({placeholders}) AND country = ? AND last_updated >= ?",
        [*thing_ids, country, cutoff],
    ).fetchall()
    out = {}
    for tid, data in rows:
        parsed = json.loads(data)
        out[tid] = (parsed["listing"], parsed["count"])
    return out


def save_market_listing_cache(conn, thing_id, country, listing, count):
    conn.execute(
        "INSERT INTO market_listings (thing_id, country, data, last_updated) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(thing_id, country) DO UPDATE SET data=excluded.data, last_updated=excluded.last_updated",
        (thing_id, country, json.dumps({"listing": listing, "count": count}), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def ensure_logged_in(playwright, auth_state_path):
    """Return a logged-in browser context, reusing a saved session if valid,
    otherwise opening a real window for the user to log in manually."""
    if Path(auth_state_path).exists():
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=auth_state_path, user_agent=HEADERS["User-Agent"])
        if _has_login_cookie(context):
            print("Reusing saved login session.")
            return browser, context
        print("Saved session looks expired — logging in again.")
        context.close()
        browser.close()

    print("Opening a browser window — please log into BGG there. Waiting for you to finish...")
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(user_agent=HEADERS["User-Agent"])
    page = context.new_page()
    page.goto("https://boardgamegeek.com/login")

    waited = 0
    while not _has_login_cookie(context):
        time.sleep(2)
        waited += 2
        if waited >= 300:
            raise RuntimeError("Timed out waiting for login (5 minutes). Run again when ready to log in.")
    print("Login detected.")
    context.storage_state(path=auth_state_path)
    page.close()
    return browser, context


def _has_login_cookie(context):
    cookie_names = {c["name"] for c in context.cookies()}
    return any(name in cookie_names for name in LOGIN_COOKIE_NAMES)


def fetch_wishlist(context, username):
    """Fetch the user's wishlist via the collection endpoint (no app token
    needed for downloading your own collection while logged in). Handles
    BGG's documented 202 'request queued, try again' behavior."""
    params = {"username": username, "wishlist": "1", "stats": "1"}

    for attempt in range(15):
        resp = context.request.get(COLLECTION_URL, params=params, timeout=30000)
        if resp.status == 202:
            print(f"  collection request queued by BGG, waiting... (attempt {attempt + 1}/15)")
            time.sleep(5)
            continue
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status {resp.status} fetching collection: {resp.text()[:300]!r}")
        body = resp.text()
        if not body.strip().startswith("<"):
            raise RuntimeError(f"Unexpected non-XML response: {body[:300]!r}")

        root = ET.fromstring(body)
        items = []
        for item in root.findall("item"):
            name_el = item.find("name")
            stats = item.find("stats")
            ratings = stats.find("rating") if stats is not None else None
            status = item.find("status")

            average = _attr(ratings, None, "average") if ratings is not None else None
            bayesaverage = _attr(ratings, None, "bayesaverage") if ratings is not None else None

            items.append({
                "thing_id": item.get("objectid"),
                "name": name_el.text if name_el is not None else None,
                "wishlist_priority": status.get("wishlistpriority") if status is not None else None,
                "average_rating": float(average) if average else None,
                "bayes_rating": float(bayesaverage) if bayesaverage else None,
            })
        return items

    raise RuntimeError("Gave up waiting for BGG to process the collection request (15 attempts).")


def _attr(el, tag, attr_name):
    if el is None:
        return None
    target = el.find(tag) if tag else el
    return target.get(attr_name) if target is not None else None


def fetch_cheapest_gb_listing(context, page, thing_id, country="GB"):
    """Load the marketplace browse page filtered to one specific game and
    capture the JSON response (same technique as bgg_market_scraper.py),
    returning the cheapest current listing, if any."""
    url = (f"{MARKET_API_URL}?objecttype=thing&objectid={thing_id}&browsetype=browse"
           f"&country={country}&sort=lowprice&displaymode=list&pageid=1")

    captured = []

    def handle_response(response, _captured=captured):
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            _captured.append(response.json())
        except Exception:
            pass

    page.on("response", handle_response)
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.remove_listener("response", handle_response)

    products = None
    for data in captured:
        found = _find_products_list(data)
        if found:
            products = found
            break

    if not products:
        return None, 0

    def price_val(p):
        try:
            return float(p.get("price", "inf"))
        except (TypeError, ValueError):
            return float("inf")

    cheapest = min(products, key=price_val)
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cheapest.get("currency"), "")
    return {
        "price_raw": f"{symbol}{cheapest.get('price')}",
        "condition": cheapest.get("prettycondition") or cheapest.get("condition"),
        "seller": (cheapest.get("linkeduser") or {}).get("username"),
        "listing_url": f"https://boardgamegeek.com/market/product/{cheapest['productid']}",
    }, len(products)


def _find_products_list(data, _depth=0):
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


BGP_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bgp_offers (
    thing_id TEXT PRIMARY KEY,
    price_raw TEXT,
    stock TEXT,
    item_url TEXT,
    last_updated TEXT
)
"""
# Columns added after the initial release — migrated in with ALTER TABLE
# below, since CREATE TABLE IF NOT EXISTS won't add columns to a table that
# already exists from an older version of this script.
BGP_CACHE_EXTRA_COLUMNS = {
    "product_price_raw": "TEXT",
    "shipping_raw": "TEXT",
    "shipping_known": "INTEGER",
    "language": "TEXT",
}


def _migrate_bgp_cache(conn):
    conn.execute(BGP_CACHE_SCHEMA)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(bgp_offers)")}
    for col, col_type in BGP_CACHE_EXTRA_COLUMNS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE bgp_offers ADD COLUMN {col} {col_type}")
    conn.commit()


def _fmt_money(value, currency):
    if value is None:
        return None
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency)
    return f"{symbol}{value}" if symbol else f"{value} {currency}"


# Values people might reasonably pass for --bgp-language "English", matched
# case-insensitively against the language code(s) in an item's versions
# dict. Confirmed from a live response: versions looks like {"lang": ["GB"]}
# — English editions are labeled "GB" (the docs warn: "yes, those are
# countries, but should be languages").
LANGUAGE_ALIASES = {
    "english": {"english", "en", "eng", "gb", "uk"},
}


def _item_language(item):
    """Return the list of language codes from an item's versions dict, e.g.
    {"lang": ["GB"]} -> ["GB"]. Returns None if versions/lang is missing.
    """
    versions = item.get("versions")
    if not isinstance(versions, dict):
        return None
    langs = versions.get("lang")
    return langs if langs else None


def _language_matches(item_langs, wanted_language):
    if not item_langs:
        return True  # no version info at all — don't exclude, likely the base/default listing
    accepted = LANGUAGE_ALIASES.get(wanted_language.lower(), {wanted_language.lower()})
    return any(str(lang).lower() in accepted for lang in item_langs)


def fetch_boardgameprices(thing_ids, sitename, currency="GBP", destination="GB",
                           cache_db="bgp_cache.db", refresh_hours=24, batch_size=40,
                           language="English", dump_json=False):
    """Look up cheapest current price + stock status per game from
    BoardGamePrices.co.uk's public API (https://boardgameprices.co.uk/api/plugin).
    Caches results locally — their API terms ask for at least an hour of
    caching, so refresh_hours defaults well above that minimum.

    A single BGG id can map to multiple items on BoardGamePrices (different
    language editions — versions look like {"lang": ["GB"]}, confirmed from
    a live response; "GB" means the English edition). Only items matching
    `language` (case-insensitive, matched flexibly — see LANGUAGE_ALIASES)
    are considered; items with no version info at all are kept (nothing to
    exclude them on). Pass dump_json=True to save one raw response to
    bgp_api_sample.json if results look off again in the future.
    """
    ids = list(dict.fromkeys(thing_ids))
    conn = sqlite3.connect(cache_db)
    _migrate_bgp_cache(conn)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=refresh_hours)).isoformat()
    placeholders = ",".join("?" * len(ids)) if ids else "''"
    rows = conn.execute(
        f"SELECT * FROM bgp_offers WHERE thing_id IN ({placeholders}) AND last_updated >= ? AND language = ?",
        [*ids, cutoff, language],
    ).fetchall() if ids else []
    cols = [c[1] for c in conn.execute("PRAGMA table_info(bgp_offers)")]
    cached = {dict(zip(cols, row))["thing_id"]: dict(zip(cols, row)) for row in rows}

    to_fetch = [tid for tid in ids if tid not in cached]
    print(f"[bgp cache] {len(cached)} games served from cache, {len(to_fetch)} need fetching")

    results = {tid: {k: v for k, v in row.items() if k not in ("thing_id", "last_updated", "language")}
               for tid, row in cached.items()}

    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i:i + batch_size]
        params = {
            "eid": ",".join(batch),
            "sitename": sitename,
            "currency": currency,
            "destination": destination,
            "sort": "CHEAP1",  # total price including shipping, cheapest first
        }
        print(f"[bgp api] fetching prices for {len(batch)} games "
              f"({i + len(batch)}/{len(to_fetch)})")
        try:
            resp = requests.get(BGP_API_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  boardgameprices.co.uk lookup failed for this batch: {e}")
            continue

        if dump_json and i == 0:
            Path("bgp_api_sample.json").write_text(json.dumps(data, indent=2)[:20000])
            print("  wrote sample response to bgp_api_sample.json")

        # A single BGG id ("eid") can map to multiple items (different
        # editions/versions — including different language editions). Skip
        # items that aren't the requested language, then aggregate offers
        # per BGG id.
        #
        # Important: BGP aggregates offers from stores in multiple countries
        # for the same edition. Offer-country preference and language/edition
        # filtering are two separate concerns. We prefer offers from the
        # requested destination country first (e.g. GB); only if there are no
        # destination-country offers for that item do we fall back to offers
        # from other countries. Within whichever offer set is chosen, prefer
        # in-stock offers; otherwise pick the cheapest.
        by_thing_id = {}
        skipped_wrong_language = 0
        for item in data.get("items", []):
            tid = str(item.get("external_id") or "")
            if not tid:
                continue
            if not _language_matches(_item_language(item), language):
                skipped_wrong_language += 1
                continue

            # Partition offers into destination-country and other-country.
            dest_offers = []
            other_offers = []
            for offer in item.get("prices", []) or []:
                if offer.get("price") is None:
                    continue
                if (offer.get("country") or "").upper() == destination.upper():
                    dest_offers.append(offer)
                else:
                    other_offers.append(offer)
            # Prefer destination-country offers; fall back only if none exist.
            preferred_offers = dest_offers if dest_offers else other_offers

            for offer in preferred_offers:
                price = offer.get("price")
                in_stock = offer.get("stock") == "Y"
                candidate = {
                    "_price": float(price),
                    "_in_stock": in_stock,
                    "price_raw": _fmt_money(price, currency),
                    "product_price_raw": _fmt_money(offer.get("product"), currency),
                    "shipping_raw": (_fmt_money(offer.get("shipping"), currency)
                                     if offer.get("shipping_known") else None),
                    "shipping_known": bool(offer.get("shipping_known")),
                    "stock": offer.get("stock") or None,
                    "item_url": item.get("url"),  # BGP's own item page, not the store's offer link
                }
                existing = by_thing_id.get(tid)
                if existing is None:
                    by_thing_id[tid] = candidate
                    continue
                # An in-stock candidate always beats a non-in-stock one,
                # regardless of price; otherwise cheapest wins within the
                # same stock tier.
                if in_stock and not existing["_in_stock"]:
                    by_thing_id[tid] = candidate
                elif in_stock == existing["_in_stock"] and candidate["_price"] < existing["_price"]:
                    by_thing_id[tid] = candidate

        if skipped_wrong_language:
            print(f"  skipped {skipped_wrong_language} item(s) not matching language={language!r}")

        now = datetime.now(timezone.utc).isoformat()
        empty_row = {"price_raw": None, "product_price_raw": None, "shipping_raw": None,
                     "shipping_known": False, "stock": None, "item_url": None}
        for tid in batch:
            row = by_thing_id.get(tid, empty_row)
            row = {k: v for k, v in row.items() if not k.startswith("_")}
            results[tid] = row
            conn.execute(
                "INSERT INTO bgp_offers (thing_id, price_raw, product_price_raw, shipping_raw, "
                "shipping_known, stock, item_url, language, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thing_id) DO UPDATE SET price_raw=excluded.price_raw, "
                "product_price_raw=excluded.product_price_raw, shipping_raw=excluded.shipping_raw, "
                "shipping_known=excluded.shipping_known, stock=excluded.stock, "
                "item_url=excluded.item_url, language=excluded.language, last_updated=excluded.last_updated",
                (tid, row["price_raw"], row["product_price_raw"], row["shipping_raw"],
                 int(row["shipping_known"]), row["stock"], row["item_url"], language, now),
            )
        conn.commit()

    conn.close()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True, help="Your BGG username")
    ap.add_argument("--country", default="GB")
    ap.add_argument("--out", default="bgg_wishlist_prices.json")
    ap.add_argument("--auth-state", default=AUTH_STATE_FILE)
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between per-game market lookups")
    ap.add_argument("--cache-db", default="bgg_wishlist_cache.db",
                    help="SQLite file caching your wishlist snapshot and per-game GeekMarket lookups")
    ap.add_argument("--wishlist-refresh-hours", type=float, default=12,
                    help="Reuse a cached wishlist snapshot younger than this many hours; 0 forces a re-fetch")
    ap.add_argument("--market-refresh-hours", type=float, default=6,
                    help="Reuse cached GeekMarket listing data younger than this many hours; 0 forces a re-fetch")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Ignore all caches (wishlist, GeekMarket, and BoardGamePrices) and fetch everything fresh")
    ap.add_argument("--skip-boardgameprices", action="store_true",
                    help="Don't look up BoardGamePrices.co.uk prices/stock")
    ap.add_argument("--bgp-sitename", default="personal-bgg-wishlist-script",
                    help="Identifier BoardGamePrices.co.uk's API asks for (their API requires 'a url to your site' "
                         "— any identifying string works for a personal script)")
    ap.add_argument("--bgp-cache-db", default="bgp_cache.db")
    ap.add_argument("--bgp-refresh-hours", type=float, default=24,
                    help="Reuse cached BoardGamePrices data younger than this many hours "
                         "(their API terms ask for at least 1 hour of caching)")
    ap.add_argument("--bgp-language", default="English",
                    help="Only consider BoardGamePrices offers for this language edition "
                         "(items with no version/language info are still included)")
    ap.add_argument("--bgp-dump-json", action="store_true",
                    help="Save one raw BoardGamePrices API response to bgp_api_sample.json "
                         "(useful for checking the actual 'lang' value format)")
    ap.add_argument("--sync-dir", default=None,
                    help="After writing results, also copy the output JSON and viewer HTML into this folder "
                         "(e.g. a Google Drive / iCloud Drive / Dropbox synced folder) so they show up on "
                         "other devices automatically — no server or network access to this machine needed "
                         "to view them.")
    args = ap.parse_args()

    wishlist_refresh_hours = 0 if args.force_refresh else args.wishlist_refresh_hours
    market_refresh_hours = 0 if args.force_refresh else args.market_refresh_hours
    bgp_refresh_hours = 0 if args.force_refresh else args.bgp_refresh_hours

    conn = sqlite3.connect(args.cache_db)
    conn.executescript(WISHLIST_CACHE_SCHEMA)
    conn.commit()

    wishlist = get_cached_wishlist(conn, args.username, wishlist_refresh_hours)
    if wishlist is not None:
        print(f"Using cached wishlist for {args.username} ({len(wishlist)} games) — "
              f"younger than {wishlist_refresh_hours}h old.")

    # Figure out which games (if any) need a fresh GeekMarket lookup. If the
    # wishlist itself is cached, we can check this before deciding whether
    # we need to open a browser at all.
    cached_listings = {}
    thing_ids_needing_market = None
    if wishlist is not None:
        thing_ids = [g["thing_id"] for g in wishlist]
        cached_listings = get_cached_market_listings(conn, thing_ids, args.country, market_refresh_hours)
        thing_ids_needing_market = [tid for tid in thing_ids if tid not in cached_listings]

    need_browser = wishlist is None or (thing_ids_needing_market and len(thing_ids_needing_market) > 0)

    results = []
    if not need_browser:
        print("Wishlist and all GeekMarket listings are fresh in cache — skipping browser/login entirely.")
        for game in wishlist:
            listing, count = cached_listings[game["thing_id"]]
            results.append({**game, "cheapest_listing": listing, "listings_available": count})
    else:
        with sync_playwright() as p:
            browser, context = ensure_logged_in(p, args.auth_state)

            if wishlist is None:
                print(f"\nFetching wishlist for {args.username}...")
                wishlist = fetch_wishlist(context, args.username)
                print(f"Found {len(wishlist)} games on your wishlist.\n")
                save_wishlist_cache(conn, args.username, wishlist)
                thing_ids = [g["thing_id"] for g in wishlist]
                cached_listings = get_cached_market_listings(conn, thing_ids, args.country, market_refresh_hours)

            page = context.new_page()
            for i, game in enumerate(wishlist, 1):
                tid = game["thing_id"]
                if tid in cached_listings:
                    listing, count = cached_listings[tid]
                    print(f"[{i}/{len(wishlist)}] {game['name']} (cached)")
                else:
                    print(f"[{i}/{len(wishlist)}] {game['name']}")
                    listing, count = fetch_cheapest_gb_listing(context, page, tid, args.country)
                    save_market_listing_cache(conn, tid, args.country, listing, count)
                    time.sleep(args.delay)
                results.append({**game, "cheapest_listing": listing, "listings_available": count})

            page.close()
            browser.close()

    conn.close()

    if not args.skip_boardgameprices:
        print()
        thing_ids = [r["thing_id"] for r in results]
        bgp_data = fetch_boardgameprices(
            thing_ids, sitename=args.bgp_sitename,
            cache_db=args.bgp_cache_db, refresh_hours=bgp_refresh_hours,
            language=args.bgp_language, dump_json=args.bgp_dump_json,
        )
        for r in results:
            r["boardgameprices"] = bgp_data.get(r["thing_id"])

    Path(args.out).write_text(json.dumps(results, indent=2))
    found = sum(1 for r in results if r["cheapest_listing"])
    bgp_found = sum(1 for r in results if r.get("boardgameprices") and r["boardgameprices"].get("price_raw"))
    print(f"\nWrote {len(results)} games to {args.out}")
    print(f"  {found} have at least one {args.country} GeekMarket listing right now")
    if not args.skip_boardgameprices:
        print(f"  {bgp_found} have a BoardGamePrices.co.uk price found")

    if args.sync_dir:
        sync_dir = Path(args.sync_dir).expanduser()
        sync_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.out, sync_dir / "bgg_wishlist_prices.json")
        viewer_src = Path(__file__).parent / "bgg_wishlist_viewer.html"
        if viewer_src.exists():
            shutil.copy2(viewer_src, sync_dir / "bgg_wishlist_viewer.html")
        else:
            print(f"  note: couldn't find bgg_wishlist_viewer.html next to this script to copy — "
                  f"copy it into {sync_dir} manually (only needs doing once, it rarely changes).")
        print(f"  synced to {sync_dir} — should appear on your other devices shortly.")
    else:
        print("Open bgg_wishlist_viewer.html and load this file to browse it.")


if __name__ == "__main__":
    main()
