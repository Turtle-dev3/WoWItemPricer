# WoW Commodity Price Tracker

Logs the current region-wide price of stackable **commodity** items from the
World of Warcraft Auction House (via the Blizzard API) into a CSV, and renders a
price chart for the last N days.

## How it works (and one important caveat)

Blizzard's API only exposes a **current snapshot** of the Auction House, updated
roughly hourly. It does **not** provide any price history. So this tool builds
history itself: every time it runs it appends one row per item to
`data/prices.csv`, and that growing CSV is your permanent record. The chart is
just a render of the last few days of it.

On day one the chart has a single point. After a week of daily runs you have a
proper 7-day line, and it keeps going for as long as you run it.

Commodities are stackable items priced **region-wide** (e.g. crafting reagents,
potions, gems). Gear and other unique items are not commodities and won't be
found by this tool.

## Setup

### 1. Get Blizzard API credentials (free)

1. Go to <https://develop.battle.net/access/clients> and log in with your
   Battle.net account.
2. Create a client. Name it anything; the redirect URL can be
   `https://localhost`.
3. Copy the **Client ID** and **Client Secret**.

### 2. Run locally (optional, for testing)

```bash
pip install -r requirements.txt
cp .env.example .env          # then put your ID/secret in .env
python wow_price_tracker.py 210796 --region eu
```

You'll see the current price printed, a row added to `data/prices.csv`, and a
PNG written to `charts/eu_210796.png`.

### 3. Run daily on GitHub Actions (the recommended setup)

The repo includes `.github/workflows/track.yml`, which runs the script on a
schedule and **commits the updated CSV and charts back into the repo**, so your
history lives in git permanently with no always-on machine required.

1. Push this repo to GitHub.
2. In the repo: **Settings -> Secrets and variables -> Actions -> New
   repository secret**, and add two secrets:
   - `BLIZZARD_CLIENT_ID`
   - `BLIZZARD_CLIENT_SECRET`
3. Edit `items.txt` to list the item IDs you want to track (one per line).
4. (Optional) Adjust the schedule in `track.yml`. It's set to `0 10 * * *`.
   **GitHub cron is UTC and ignores daylight saving**, so:
   - `0 10 * * *` = 12:00 in Denmark during summer (CEST)
   - `0 11 * * *` = 12:00 in Denmark during winter (CET)

That's it. You can also trigger a run manually from the **Actions** tab
("Run workflow"), optionally passing item IDs for a one-off.

> Note: scheduled Actions can be delayed by a few minutes under load, and
> GitHub auto-disables schedules after 60 days with no repo activity, but the
> daily commit this workflow makes counts as activity, so it stays alive.

### Alternative: cron on your own server

If you'd rather run it on your Hetzner box instead of GitHub Actions:

```bash
# crontab -e  (this runs at 12:00 server time daily)
0 12 * * * cd /path/to/wow-price-tracker && /usr/bin/python3 wow_price_tracker.py 210796 213612 --region eu >> tracker.log 2>&1
```

Put the credentials in a `.env` file next to the script (or export them in the
crontab environment). The CSV just accumulates on the server; push it to git
yourself if you also want it versioned.

## CSV format

| column                  | meaning                                              |
|-------------------------|------------------------------------------------------|
| `timestamp_utc`         | when the run happened (ISO 8601, UTC)                |
| `item_id`               | the item ID                                          |
| `item_name`             | resolved name (best effort)                          |
| `region`                | eu / us / kr / tw                                    |
| `min_unit_price_copper` | lowest unit price on offer, in copper (the "going rate") |
| `total_quantity`        | total units listed across all auctions               |
| `num_auctions`          | how many auction stacks were listed                  |

Prices are stored in **copper**; divide by 10,000 for gold.

## Command-line options

```
python wow_price_tracker.py ITEM_ID [ITEM_ID ...]
  --region {eu,us,kr,tw}   default: eu
  --days N                 chart window in days (default: 7)
  --csv PATH               default: data/prices.csv
  --chart-dir DIR          default: charts
  --no-chart               log to CSV only, skip chart
```
