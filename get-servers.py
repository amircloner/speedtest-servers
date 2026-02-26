#!/usr/bin/env python3
"""
Fetch Speedtest.net server list for every country/city pair.

Features:
  - Local JSON database (db.json) that persists results incrementally.
  - Concurrent requests (10 at a time) via asyncio + curl.
  - Resume support: already-fetched cities are skipped on restart.
  - Cities are processed in alphabetical order.
"""

import asyncio
import csv
import json
import sys
import urllib.parse
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
CITY_PAIRS_FILE = Path("country_city_pairs.csv")
DB_FILE = Path("db.json")
OUTPUT_JSON = Path("speedtest_servers.json")
OUTPUT_CSV = Path("speedtest_servers.csv")

CONCURRENCY = 10  # number of parallel requests


# ── local JSON database ─────────────────────────────────────────────────────
def load_db() -> dict:
    """Load the local database.  Structure:
    {
        "completed": {"City|Country": true, ...},
        "servers": { "<server_id>": { ...server data... }, ... }
    }
    """
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "servers": {}}


def save_db(db: dict) -> None:
    """Atomically save the database (write to tmp then rename)."""
    tmp = DB_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.rename(DB_FILE)


# ── read input CSV ──────────────────────────────────────────────────────────
def read_city_pairs(path: Path) -> list[tuple[str, str]]:
    """Return sorted list of (city, country) pairs from the CSV."""
    pairs: set[tuple[str, str]] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) >= 2:
                pairs.add((row[0].strip(), row[1].strip()))
    # sort alphabetically by city, then country
    return sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))


# ── single fetch (curl) ─────────────────────────────────────────────────────
async def fetch_city(city: str, country: str, retries: int = 3) -> tuple[str, str, list[dict]]:
    """Fetch servers for one city using curl (avoids Python TLS issues).
    Returns (city, country, servers_list)."""
    encoded = urllib.parse.quote(city)
    url = (
        "https://www.speedtest.net/api/js/servers"
        f"?engine=js&https_functional=true&limit=100&search={encoded}"
    )

    for attempt in range(1, retries + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "--max-time", "30",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept: application/json, text/plain, */*",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-H", "Referer: https://www.speedtest.net/",
                "-w", "\n%{http_code}",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
            output = stdout.decode("utf-8", errors="replace")
            parts = output.rsplit("\n", 1)
            body = parts[0] if len(parts) > 1 else ""
            status_str = parts[-1].strip()
            status = int(status_str) if status_str.isdigit() else 0

            if status == 200 and body:
                return city, country, json.loads(body)
            if status == 403:
                await asyncio.sleep(5 * attempt)
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
            if attempt < retries:
                await asyncio.sleep(3 * attempt)
    return city, country, []


# ── batch processing ────────────────────────────────────────────────────────
async def process_batch(batch: list[tuple[str, str]], db: dict) -> int:
    """Process a batch of city pairs concurrently. Returns count of new servers."""
    tasks = [fetch_city(city, country) for city, country in batch]
    results = await asyncio.gather(*tasks)

    new_count = 0
    for city, country, servers in results:
        key = f"{city}|{country}"
        for srv in servers:
            sid = str(srv.get("id", ""))
            if sid and sid not in db["servers"]:
                db["servers"][sid] = srv
                new_count += 1
        db["completed"][key] = True

    # persist after each batch
    save_db(db)
    return new_count


async def main() -> None:
    if not CITY_PAIRS_FILE.exists():
        print(f"Error: {CITY_PAIRS_FILE} not found.")
        sys.exit(1)

    db = load_db()
    all_pairs = read_city_pairs(CITY_PAIRS_FILE)

    # filter out already-completed pairs
    pending = [(c, co) for c, co in all_pairs if f"{c}|{co}" not in db["completed"]]
    done_count = len(all_pairs) - len(pending)
    total = len(all_pairs)

    print(f"Total city pairs : {total}")
    print(f"Already completed: {done_count}")
    print(f"Remaining        : {len(pending)}")
    print(f"Servers in DB    : {len(db['servers'])}")
    print(f"Concurrency      : {CONCURRENCY}")
    print("-" * 50)

    if not pending:
        print("All cities already fetched! Exporting...")
    else:
        batches = [pending[i : i + CONCURRENCY] for i in range(0, len(pending), CONCURRENCY)]
        total_batches = len(batches)

        for i, batch in enumerate(batches, 1):
            cities_str = ", ".join(c for c, _ in batch)
            print(f"\n[Batch {i}/{total_batches}] {cities_str}")

            new = await process_batch(batch, db)
            processed = done_count + i * CONCURRENCY
            progress = min(processed, total)

            print(
                f"  +{new} new servers | "
                f"Progress: {progress}/{total} "
                f"({progress * 100 // total}%) | "
                f"DB total: {len(db['servers'])}"
            )

            # small delay between batches to avoid rate limiting
            if i < total_batches:
                await asyncio.sleep(2)

    # ── export final outputs ─────────────────────────────────────────────
    all_servers = list(db["servers"].values())
    print(f"\nExporting {len(all_servers)} servers...")

    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_servers, f, ensure_ascii=False, indent=4)
    print(f"  -> {OUTPUT_JSON}")

    # CSV
    if all_servers:
        all_keys = set()
        for s in all_servers:
            all_keys.update(s.keys())
        fieldnames = sorted(all_keys)

        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_servers)
        print(f"  -> {OUTPUT_CSV}")

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())