#!/usr/bin/env python3
"""
Fuel Price Tracker — automatic price updater
=============================================
Runs on GitHub Actions every night. Reads public sources that publish the
OGRA / Ministry of Energy (Petroleum Division) notified ex-depot prices,
extracts the petrol (MS) and high-speed diesel (HSD) rates, and rewrites
prices.json if the rates have genuinely changed.

Safety rules (so a broken website can never corrupt the app):
  * a value is only accepted if it appears on at least MIN_AGREE sources
  * prices must fall inside SANE_MIN..SANE_MAX
  * a jump larger than MAX_JUMP from the last known price is rejected
  * if anything is uncertain the script exits without writing

Manual override: run the workflow from the Actions tab and type the prices
in by hand. That path skips all scraping.
"""

import json
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES = ROOT / "prices.json"

PKT = timezone(timedelta(hours=5))

# ---------------------------------------------------------------- settings
SOURCES = [
    "https://www.ogra.org.pk/",
    "https://petrolprice.com.pk/",
    "https://www.geo.tv/latest/673967-petroleum-rates-in-pakistan-today",
    "https://arynews.tv/category/business/",
    "https://en.dailypakistan.com.pk/petrol-price/",
]

PETROL_WORDS = ["motor spirit", "petrol", "ms price", "(ms)"]
DIESEL_WORDS = ["high speed diesel", "high-speed diesel", "diesel", "hsd"]

WINDOW    = 260     # characters to search around a keyword
MIN_AGREE = 2       # how many sources must report the same number
SANE_MIN  = 120.0   # a litre will not cost less than this
SANE_MAX  = 900.0   # ...nor more than this
MAX_JUMP  = 60.0    # biggest believable one-day move, in rupees

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            return raw.decode("utf-8", "ignore")
        except Exception:
            return raw.decode("latin-1", "ignore")
    except Exception as e:
        log(f"  ! could not read {url}: {e}")
        return ""


def to_text(html):
    """Strip tags and collapse whitespace so keywords sit near their numbers."""
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#8217;", "'").replace("&rsquo;", "'"))
    return re.sub(r"\s+", " ", html).lower()


NUM = re.compile(r"(?<![\d.])(\d{3}\.\d{2})(?!\d)")


def candidates(text, words, rival_words):
    """
    Prices appearing after one of `words` but before the next mention of a
    rival product. Within that segment the LAST number is taken, because
    reports are written as "raised from 327.12 to 331.52" — the new rate
    comes second.
    """
    found = []
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i == -1:
                break
            start = i + len(w)

            segment = text[i + len(w): i + len(w) + WINDOW]

            # cut the segment short at the first rival product mention
            cut = len(segment)
            for rv in rival_words:
                j = segment.find(rv)
                if j != -1:
                    cut = min(cut, j)
            segment = segment[:cut]

            vals = [float(m.group(1)) for m in NUM.finditer(segment)]
            vals = [v for v in vals if SANE_MIN <= v <= SANE_MAX]
            if vals:
                found.append(vals[-1])
    return found


def agreed(per_source):
    """Pick the value that the most independent sources reported."""
    votes = Counter()
    for vals in per_source:
        for v in set(vals):
            votes[v] += 1
    if not votes:
        return None, 0
    value, count = votes.most_common(1)[0]
    return value, count


# ---------------------------------------------------------------- main
def scrape():
    petrol_per_source, diesel_per_source = [], []

    for url in SOURCES:
        log(f"  reading {url}")
        text = to_text(fetch(url))
        if not text:
            continue
        p = candidates(text, PETROL_WORDS, DIESEL_WORDS)
        d = candidates(text, DIESEL_WORDS, PETROL_WORDS)
        if p:
            petrol_per_source.append(p)
        if d:
            diesel_per_source.append(d)

    petrol, pv = agreed(petrol_per_source)
    diesel, dv = agreed(diesel_per_source)
    log(f"  petrol candidate {petrol} (agreed by {pv}) | "
        f"diesel candidate {diesel} (agreed by {dv})")

    if petrol is None or pv < MIN_AGREE:
        log("  → not enough agreement on petrol; skipping")
        return None, None
    if diesel is None or dv < MIN_AGREE:
        log("  → not enough agreement on diesel; skipping")
        return None, None
    if abs(petrol - diesel) < 0.01:
        log("  → petrol and diesel came out identical; suspicious, skipping")
        return None, None

    return petrol, diesel


def main():
    data = json.loads(PRICES.read_text(encoding="utf-8"))
    ms  = data["products"]["ms"]["history"]
    hsd = data["products"]["hsd"]["history"]
    last_ms, last_hsd = ms[0]["price"], hsd[0]["price"]

    manual_ms  = os.environ.get("MANUAL_PETROL", "").strip()
    manual_hsd = os.environ.get("MANUAL_DIESEL", "").strip()

    if manual_ms and manual_hsd:
        log("Manual prices supplied — skipping the websites.")
        try:
            petrol, diesel = float(manual_ms), float(manual_hsd)
        except ValueError:
            log("Manual values are not numbers. Nothing written.")
            return 1
    else:
        log("Checking public sources for today's notified rates…")
        petrol, diesel = scrape()
        if petrol is None:
            log("No confident reading. prices.json left untouched.")
            return 0

        if abs(petrol - last_ms) > MAX_JUMP or abs(diesel - last_hsd) > MAX_JUMP:
            log(f"Change too large to trust "
                f"(petrol {last_ms}→{petrol}, diesel {last_hsd}→{diesel}). "
                f"Left untouched — update by hand if this is real.")
            return 0

    if abs(petrol - last_ms) < 0.005 and abs(diesel - last_hsd) < 0.005:
        log("Prices are unchanged. Nothing to commit.")
        return 0

    today = datetime.now(PKT).strftime("%Y-%m-%d")

    def push(hist, price):
        if hist and hist[0]["date"] == today:
            hist[0]["price"] = price
        else:
            hist.insert(0, {"date": today, "price": price})
        del hist[40:]

    push(ms, petrol)
    push(hsd, diesel)

    data["effective"] = today
    data["updatedAt"] = datetime.now(PKT).isoformat(timespec="seconds")
    data["sourceNote"] = (
        "Ex-depot prices notified by the Ministry of Energy (Petroleum Division) "
        "on the advice of OGRA under the daily petroleum pricing mechanism, "
        f"effective {today}. Collected automatically from public sources; "
        "verify against the official notification for any formal use."
    )

    PRICES.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")

    log(f"Updated: petrol {last_ms} → {petrol}, diesel {last_hsd} → {diesel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
