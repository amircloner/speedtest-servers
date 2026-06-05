#!/usr/bin/env python3
"""
Fetch Speedtest.net server list for every country/city pair.

12-Factor compliance:
  III  – Config from environment via config.py
  IV   – DB_FILE / CITY_PAIRS_FILE / OUTPUT paths as backing-service URLs
  XI   – All output via logging (stdout), no log files
  XII  – Admin process: run as a one-off task
         e.g.  docker compose run --rm speedtest-gui python get-servers.py

Features:
  - Local JSON database (db.json) that persists results incrementally.
  - Concurrent requests (SPEEDTEST_CONCURRENCY at a time) via asyncio + curl.
  - Resume support: already-fetched cities are skipped on restart.
  - Cities are processed in alphabetical order.
"""

import asyncio
import csv
import json
import logging
import sys
import urllib.parse
from pathlib import Path

import config
from logging_config import configure_logging

# ── Logging (Factor XI) ───────────────────────────────────────────────────────
configure_logging()
log = logging.getLogger(__name__)

# ── Output paths (Factor IV – treat as backing-service locations) ─────────────
OUTPUT_JSON = Path("speedtest_servers.json")
OUTPUT_CSV  = Path("speedtest_servers.csv")


# ── Local JSON database ───────────────────────────────────────────────────────

def load_db() -> dict:
    """Load the local database.
    Structure:
        {
            "completed": {"City|Country": true, ...},
            "servers":   {"<server_id>": {...}, ...}
        }
    """
    if config.DB_FILE.exists():
        with open(config.DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "servers": {}}


def save_db(db: dict) -> None:
    """Atomically save the database (write to tmp then rename)."""
    tmp = config.DB_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.rename(config.DB_FILE)
    log.debug("DB saved (%d servers)", len(db["servers"]))


# ── Read input CSV ─────────────────────────────────────────────────────────────

def read_city_pairs(path: Path) -> list[tuple[str, str]]:
    """Return sorted list of (city, country) pairs from the CSV."""
    pairs: set[tuple[str, str]] = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            if len(row) >= 2:
                pairs.add((row[0].strip(), row[1].strip()))
    return sorted(pairs, key=lambda p: (p[0].lower(), p[1].lower()))


# ── Single fetch (curl) ────────────────────────────────────────────────────────

async def fetch_city(
    city: str,
    country: str,
    retries: int = config.FETCH_RETRIES,
) -> tuple[str, str, list[dict]]:
    """Fetch servers for one city using curl (avoids Python TLS issues).
    Returns (city, country, servers_list).
    """
    encoded = urllib.parse.quote(city)
    url = (
        "https://www.speedtest.net/api/js/servers"
        f"?engine=js&https_functional=true&limit=100&search={encoded}"
    )

    for attempt in range(1, retries + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s",
                "--max-time", str(config.FETCH_TIMEOUT),
                "-H", (
                    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "-H", "Accept: application/json, text/plain, */*",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-H", "Referer: https://www.speedtest.net/",
                "-w", "\n%{http_code}",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=config.FETCH_TIMEOUT + 5
            )
            output    = stdout.decode("utf-8", errors="replace")
            parts     = output.rsplit("\n", 1)
            body      = parts[0] if len(parts) > 1 else ""
            status_str = parts[-1].strip()
            status    = int(status_str) if status_str.isdigit() else 0

            if status == 200 and body:
                servers = json.loads(body)
                log.debug("fetch_city %r: HTTP 200, %d servers", city, len(servers))
                return city, country, servers

            if status == 403:
                log.warning("fetch_city %r: HTTP 403, backing off (attempt %d)", city, attempt)
                await asyncio.sleep(5 * attempt)
            else:
                log.debug("fetch_city %r: HTTP %d (attempt %d)", city, status, attempt)

        except asyncio.TimeoutError:
            log.warning("fetch_city %r: timeout (attempt %d)", city, attempt)
            if attempt < retries:
                await asyncio.sleep(3 * attempt)
        except json.JSONDecodeError as exc:
            log.warning("fetch_city %r: JSON decode error: %s", city, exc)
            if attempt < retries:
                await asyncio.sleep(3 * attempt)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_city %r: unexpected error: %s", city, exc)
            if attempt < retries:
                await asyncio.sleep(3 * attempt)

    log.warning("fetch_city %r: all %d attempts failed", city, retries)
    return city, country, []


# ── Batch processing ───────────────────────────────────────────────────────────

async def process_batch(batch: list[tuple[str, str]], db: dict) -> int:
    """Process a batch of city pairs concurrently. Returns count of new servers."""
    tasks   = [fetch_city(city, country) for city, country in batch]
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

    save_db(db)
    return new_count


# ── Main (Factor XII – admin process) ─────────────────────────────────────────

async def main() -> None:
    if not config.CITY_PAIRS_FILE.exists():
        log.error("City pairs file not found: %s", config.CITY_PAIRS_FILE)
        sys.exit(1)

    db        = load_db()
    all_pairs = read_city_pairs(config.CITY_PAIRS_FILE)

    pending    = [(c, co) for c, co in all_pairs if f"{c}|{co}" not in db["completed"]]
    done_count = len(all_pairs) - len(pending)
    total      = len(all_pairs)

    log.info("Total city pairs : %d", total)
    log.info("Already completed: %d", done_count)
    log.info("Remaining        : %d", len(pending))
    log.info("Servers in DB    : %d", len(db["servers"]))
    log.info("Concurrency      : %d", config.SPEEDTEST_CONCURRENCY)

    if not pending:
        log.info("All cities already fetched. Exporting…")
    else:
        batches       = [
            pending[i: i + config.SPEEDTEST_CONCURRENCY]
            for i in range(0, len(pending), config.SPEEDTEST_CONCURRENCY)
        ]
        total_batches = len(batches)

        for i, batch in enumerate(batches, 1):
            cities_str = ", ".join(c for c, _ in batch)
            log.info("[Batch %d/%d] %s", i, total_batches, cities_str)

            new      = await process_batch(batch, db)
            progress = min(done_count + i * config.SPEEDTEST_CONCURRENCY, total)

            log.info(
                "  +%d new servers | progress %d/%d (%d%%) | DB total: %d",
                new, progress, total,
                progress * 100 // total,
                len(db["servers"]),
            )

            if i < total_batches:
                await asyncio.sleep(config.FETCH_INTER_BATCH_DELAY)

    # ── Export final outputs ──────────────────────────────────────────────────
    all_servers = list(db["servers"].values())
    log.info("Exporting %d servers…", len(all_servers))

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_servers, f, ensure_ascii=False, indent=4)
    log.info("Exported JSON → %s", OUTPUT_JSON)

    if all_servers:
        import csv as _csv
        all_keys   = set()
        for s in all_servers:
            all_keys.update(s.keys())
        fieldnames = sorted(all_keys)

        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_servers)
        log.info("Exported CSV  → %s", OUTPUT_CSV)

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
