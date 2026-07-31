#!/usr/bin/env python3
"""
bgg_wishlist_prices.py

Finds the cheapest current UK GeekMarket listing for every game on your
BGG wishlist.

Why this doesn't need a registered API token:
- BGG's collection endpoint (xmlapi2/collection) is exempt from the "must
  register an application" rule *when downloading your own collection while
  logged in* (see https://boardgamegeek.com/using_the_xml_api). So this
  script logs you into a real browser session first, then uses that.
- The per-game marketplace price lookup uses the same private API the
  GeekMarket browse page itself uses (api.geekdo.com/api/market/products),
  which is what bgg_market_scraper.py already relies on — no login or token
  needed for that part, since it's what powers the public browse page.

Login flow:
- BGG's login page sits behind Cloudflare Turnstile, which is specifically
  designed to block scripted form-filling. So instead of automating your
  password entry, this script opens a REAL browser window, you log in
  yourself (once), and it detects the logged-in session and saves it to
  bgg_auth.json for reuse on future runs — no password ever touches this
  script.

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python bgg_wishlist_prices.py --username YOUR_BGG_USERNAME --out bgg_wishlist_prices.json
"""

import argparse
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from playwright.sync_api import sync_playwright

COLLECTION_URL = "https://boardgamegeek.com/xmlapi2/collection"
MARKET_API_URL = "https://boardgamegeek.com/market"
AUTH_STATE_FILE = "bgg_auth.json"
LOGIN_COOKIE_NAMES = ("bggusername", "SessionID")

HEADERS = {"User-Agent": "Mozilla/5.0 (personal script; BGG wishlist price checker)"}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True, help="Your BGG username")
    ap.add_argument("--country", default="GB")
    ap.add_argument("--out", default="bgg_wishlist_prices.json")
    ap.add_argument("--auth-state", default=AUTH_STATE_FILE)
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between per-game market lookups")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser, context = ensure_logged_in(p, args.auth_state)

        print(f"\nFetching wishlist for {args.username}...")
        wishlist = fetch_wishlist(context, args.username)
        print(f"Found {len(wishlist)} games on your wishlist.\n")

        page = context.new_page()
        results = []
        for i, game in enumerate(wishlist, 1):
            print(f"[{i}/{len(wishlist)}] {game['name']}")
            listing, count = fetch_cheapest_gb_listing(context, page, game["thing_id"], args.country)
            results.append({
                **game,
                "cheapest_listing": listing,
                "listings_available": count,
            })
            time.sleep(args.delay)

        page.close()
        browser.close()

    Path(args.out).write_text(json.dumps(results, indent=2))
    found = sum(1 for r in results if r["cheapest_listing"])
    print(f"\nWrote {len(results)} games to {args.out} ({found} have at least one {args.country} listing right now).")
    print("Open bgg_wishlist_viewer.html and load this file to browse it.")


if __name__ == "__main__":
    main()
