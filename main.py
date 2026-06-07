#!/usr/bin/env python3
"""
WoW commodity price tracker.

Fetches the current region-wide price of one or more stackable commodity items
from the Blizzard Auction House API, appends a row per item to a CSV, and
(re)generates a price chart from whatever history that CSV holds.

Blizzard's API exposes only a *current* snapshot, so history is built up by
running this on a schedule (a GitHub Action by default, or cron). The CSV is the
permanent record; the chart is just a render of the last N days of it.

Usage:
    python wow_price_tracker.py ITEM_ID [ITEM_ID ...] [options]

Examples:
    # Track Crystallized Fire (made-up id) on EU, default 7-day chart
    python wow_price_tracker.py 210796

    # Track several items, US region, 14-day chart window
    python wow_price_tracker.py 210796 213612 --region us --days 14

Credentials come from the environment (or a local .env file):
    BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET
Get them at https://develop.battle.net/access/clients (free).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

import requests

# Matplotlib without a display (works headless in CI / on a server).
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

OAUTH_TOKEN_URL = "https://oauth.battle.net/token"
COPPER_PER_GOLD = 10_000
VALID_REGIONS = {"eu", "us", "kr", "tw"}

CSV_FIELDS = [
    "timestamp_utc",
    "item_id",
    "item_name",
    "region",
    "min_unit_price_copper",
    "total_quantity",
    "num_auctions",
]

# Chart palette (dark, WoW-ish gold)
BG, PANEL = "#0d1117", "#161b22"
GOLD, GOLD2 = "#e6b450", "#f5d488"
TEXT, MUTE, GRID = "#c9d1d9", "#8b949e", "#30363d"


# --------------------------------------------------------------------------- #
# Tiny .env loader (so we don't need python-dotenv as a dependency)
# --------------------------------------------------------------------------- #
def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Don't clobber values already set in the real environment.
        os.environ.setdefault(key, value)


# --------------------------------------------------------------------------- #
# Blizzard API
# --------------------------------------------------------------------------- #
def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_commodities(region: str, token: str) -> list[dict]:
    """Return the full region-wide list of commodity auctions."""
    url = f"https://{region}.api.blizzard.com/data/wow/auctions/commodities"
    resp = requests.get(
        url,
        params={"namespace": f"dynamic-{region}", "locale": "en_US"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("auctions", [])


def fetch_item_name(item_id: int, region: str, token: str) -> str:
    """Best-effort item name lookup; falls back to the id on any failure."""
    url = f"https://{region}.api.blizzard.com/data/wow/item/{item_id}"
    try:
        resp = requests.get(
            url,
            params={"namespace": f"static-{region}", "locale": "en_US"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("name", str(item_id))
    except requests.RequestException:
        return str(item_id)


def summarize_item(auctions: list[dict], item_id: int) -> dict | None:
    """
    Pull the market summary for one commodity out of the full auction list.

    'Current price' = lowest unit price on offer (matches how the in-game AH and
    common addons quote the going rate). Returns None if the item isn't present
    (i.e. not a commodity, or nothing currently listed).
    """
    rows = [a for a in auctions if a.get("item", {}).get("id") == item_id]
    if not rows:
        return None
    return {
        "min_unit_price_copper": min(a["unit_price"] for a in rows),
        "total_quantity": sum(a["quantity"] for a in rows),
        "num_auctions": len(rows),
    }


# --------------------------------------------------------------------------- #
# CSV + formatting
# --------------------------------------------------------------------------- #
def copper_to_gsc(copper: int) -> str:
    """Format copper as '1,234g 56s 78c'."""
    gold, rem = divmod(int(copper), COPPER_PER_GOLD)
    silver, copper_ = divmod(rem, 100)
    return f"{gold:,}g {silver:02d}s {copper_:02d}c"


def append_row(csv_path: Path, row: dict) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def read_history(csv_path: Path, item_id: int, region: str) -> list[dict]:
    if not csv_path.exists():
        return []
    out = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if int(r["item_id"]) == item_id and r["region"] == region:
                out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Charting
# --------------------------------------------------------------------------- #
def plot_history(history: list[dict], item_id: int, item_name: str,
                 region: str, out_path: Path, days: int) -> bool:
    """Render the last `days` of price history to a PNG. Returns False if empty."""
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)

    points = []
    for r in history:
        ts = dt.datetime.fromisoformat(r["timestamp_utc"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts < cutoff:
            continue
        raw = r.get("min_unit_price_copper", "")
        if raw in ("", None):
            continue
        points.append((ts, int(raw) / COPPER_PER_GOLD))

    if not points:
        return False

    points.sort(key=lambda p: p[0])
    times = [p[0] for p in points]
    prices = [p[1] for p in points]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 5.2))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)

    ax.plot(times, prices, color=GOLD, linewidth=2.2, zorder=3,
            marker="o", markersize=6, markerfacecolor=GOLD2,
            markeredgecolor=BG, markeredgewidth=1.2)
    ax.fill_between(times, prices, 0, color=GOLD, alpha=0.10, zorder=1)

    # Annotate the most recent point.
    ax.annotate(f"{prices[-1]:,.0f}g", xy=(times[-1], prices[-1]),
                xytext=(0, 12), textcoords="offset points", ha="center",
                fontsize=10, fontweight="bold", color=GOLD2, zorder=4)

    # Pin the x-axis to the window so a single point doesn't spray across years.
    ax.set_xlim(cutoff, now)
    loc = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    # Pad the y-axis (handles the single-point case gracefully).
    lo, hi = min(prices), max(prices)
    pad = max(lo * 0.10, 1) if lo == hi else (hi - lo) * 0.15
    ax.set_ylim(max(0, lo - pad), hi + pad)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}g"))

    ax.set_title(f"{item_name}  ({item_id})  -  {region.upper()}",
                 color=TEXT, fontsize=14, fontweight="bold", pad=14)
    ax.tick_params(colors=MUTE, labelsize=10)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)

    n = len(points)
    fig.text(0.5, 0.015,
             f"min unit price | {n} data point{'s' if n != 1 else ''} "
             f"| window: last {days} day{'s' if days != 1 else ''}",
             ha="center", color=MUTE, fontsize=8.5)

    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out_path, dpi=130, facecolor=BG)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Track WoW commodity prices via the Blizzard API.")
    p.add_argument("item_ids", nargs="+", type=int, help="One or more item IDs.")
    p.add_argument("--region", default=os.environ.get("WOW_REGION", "eu"),
                   choices=sorted(VALID_REGIONS), help="Region (default: eu).")
    p.add_argument("--days", type=int, default=7,
                   help="Chart window in days (default: 7).")
    p.add_argument("--csv", default="data/prices.csv",
                   help="CSV path (default: data/prices.csv).")
    p.add_argument("--chart-dir", default="charts",
                   help="Directory for PNG charts (default: charts).")
    p.add_argument("--no-chart", action="store_true",
                   help="Only log to CSV, skip chart generation.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_dotenv()

    client_id = os.environ.get("BLIZZARD_CLIENT_ID")
    client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Missing BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET "
                 "(set them in the environment or a .env file).")

    csv_path = Path(args.csv)
    chart_dir = Path(args.chart_dir)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    print(f"Authenticating with Blizzard ({args.region.upper()})...")
    token = get_access_token(client_id, client_secret)

    print("Fetching commodities snapshot (one big region-wide pull)...")
    auctions = fetch_commodities(args.region, token)
    print(f"  {len(auctions):,} commodity auctions in the snapshot.\n")

    exit_code = 0
    for item_id in args.item_ids:
        name = fetch_item_name(item_id, args.region, token)
        summary = summarize_item(auctions, item_id)

        if summary is None:
            print(f"[{item_id}] {name}: not found in commodities snapshot. "
                  f"It may not be a stackable commodity, or nothing is listed "
                  f"right now. Logging a blank row.")
            row = {
                "timestamp_utc": now.isoformat(),
                "item_id": item_id,
                "item_name": name,
                "region": args.region,
                "min_unit_price_copper": "",
                "total_quantity": 0,
                "num_auctions": 0,
            }
            exit_code = 1
        else:
            price_c = summary["min_unit_price_copper"]
            print(f"[{item_id}] {name}: {copper_to_gsc(price_c)} "
                  f"({summary['total_quantity']:,} available across "
                  f"{summary['num_auctions']:,} auctions)")
            row = {
                "timestamp_utc": now.isoformat(),
                "item_id": item_id,
                "item_name": name,
                "region": args.region,
                "min_unit_price_copper": price_c,
                "total_quantity": summary["total_quantity"],
                "num_auctions": summary["num_auctions"],
            }

        append_row(csv_path, row)

        if not args.no_chart:
            history = read_history(csv_path, item_id, args.region)
            out_png = chart_dir / f"{args.region}_{item_id}.png"
            if plot_history(history, item_id, name, args.region, out_png, args.days):
                print(f"        chart -> {out_png} "
                      f"({len([h for h in history])} data point(s) on file)")
            else:
                print("        (not enough priced history yet for a chart)")

    print(f"\nLogged to {csv_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
