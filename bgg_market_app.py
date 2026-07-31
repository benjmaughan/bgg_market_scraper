#!/usr/bin/env python3
"""
bgg_market_app.py

Streamlit viewer for the JSON produced by bgg_market_scraper.py.
An alternative to bgg_market_viewer.html — same data, native widgets
(sliders, dropdowns) instead of a plain HTML form, and it runs as a
local Python app rather than a static file.

Setup:
    pip install streamlit pandas

Usage:
    streamlit run bgg_market_app.py -- --data bgg_market_data.json

(the "--" is required so Streamlit passes --data through to the script)
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="bgg_market_data.json")
    # Streamlit passes its own args too; only parse the ones we know.
    args, _ = ap.parse_known_args(sys.argv[1:])
    return args


def price_num(raw):
    if not raw:
        return None
    m = re.search(r"[\d.,]+", raw)
    return float(m.group(0).replace(",", "")) if m else None


def best_with_num(val):
    if not val:
        return None
    m = re.search(r"\d+", str(val))
    return int(m.group(0)) if m else None


@st.cache_data
def load_data(path):
    rows = json.loads(Path(path).read_text())
    df = pd.DataFrame(rows)
    df["price_num"] = df["price_raw"].apply(price_num)
    df["best_with_num"] = df.get("best_with", pd.Series(dtype=object)).apply(best_with_num)
    return df


st.set_page_config(page_title="GeekMarket Finder", layout="wide")
st.title("GeekMarket Finder")

args = parse_args()
data_path = Path(args.data)

if not data_path.exists():
    st.error(f"Couldn't find {data_path}. Pass a different file with: "
             f"streamlit run bgg_market_app.py -- --data your_file.json")
    st.stop()

df = load_data(str(data_path))
st.caption(f"Loaded {len(df)} listings from {data_path}")

with st.sidebar:
    st.header("Filters")

    min_rating = st.slider("Minimum rating", 0.0, 10.0, 0.0, 0.1)

    max_weight_available = float(df["weight"].dropna().max()) if df["weight"].notna().any() else 5.0
    max_weight = st.slider("Maximum weight (complexity)", 0.0, max(max_weight_available, 1.0), max(max_weight_available, 1.0), 0.1)

    min_votes = st.number_input("Minimum number of ratings", min_value=0, value=0, step=50)

    max_price_available = df["price_num"].dropna().max() if df["price_num"].notna().any() else 100
    max_price = st.slider("Maximum price (£)", 0.0, float(max_price_available), float(max_price_available))

    player_count = st.selectbox("Player count", ["Any", "1", "2", "3", "4", "5", "6", "7+"])
    best_with_only = st.checkbox("Only show games best at that count (not just supported)", value=False)

filtered = df.copy()
filtered = filtered[filtered["average_rating"].fillna(0) >= min_rating]
filtered = filtered[filtered["weight"].fillna(0) <= max_weight]
filtered = filtered[filtered["num_ratings"].fillna(0) >= min_votes]
filtered = filtered[filtered["price_num"].fillna(1e9) <= max_price]

if player_count != "Any":
    if player_count == "7+":
        n = 7
        if best_with_only:
            filtered = filtered[filtered["best_with_num"].fillna(0) >= n]
        else:
            filtered = filtered[pd.to_numeric(filtered["maxplayers"], errors="coerce").fillna(0) >= n]
    else:
        n = int(player_count)
        if best_with_only:
            filtered = filtered[filtered["best_with_num"] == n]
        else:
            mn = pd.to_numeric(filtered["minplayers"], errors="coerce")
            mx = pd.to_numeric(filtered["maxplayers"], errors="coerce")
            filtered = filtered[(mn <= n) & (mx >= n)]

st.subheader(f"{len(filtered)} of {len(df)} listings match")

display_cols = {
    "name": "Game",
    "price_raw": "Price",
    "average_rating": "Rating",
    "num_ratings": "# Ratings",
    "weight": "Weight",
    "rank": "Rank",
    "minplayers": "Min players",
    "maxplayers": "Max players",
    "best_with": "Best with",
    "playingtime": "Time (min)",
    "listing_url": "Listing",
}
existing_cols = [c for c in display_cols if c in filtered.columns]
view = filtered[existing_cols].rename(columns=display_cols)

sort_col = st.selectbox("Sort by", list(view.columns), index=list(view.columns).index("Rating") if "Rating" in view.columns else 0)
ascending = st.checkbox("Ascending", value=False)
view = view.sort_values(sort_col, ascending=ascending, na_position="last")

st.dataframe(
    view,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Listing": st.column_config.LinkColumn("Listing", display_text="view"),
    },
)
