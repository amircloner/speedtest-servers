#!/usr/bin/env python3
"""
Speedtest runner for all servers of a given country and/or sponsor.

12-Factor compliance:
  III  – Config from environment via config.py
  IV   – DB_FILE / RESULTS_DIR as backing-service paths
  XI   – All output via logging (stdout), no log files
  XII  – Admin process: run as a one-off task
         e.g.  docker compose run --rm speedtest-gui python speedtest_country.py Germany

Usage:
    python speedtest_country.py "Germany"
    python speedtest_country.py "United States"
    python speedtest_country.py --sponsor "Melbicom"
    python speedtest_country.py "Germany" --sponsor "Melbicom"
"""

import argparse
import json
import logging
import re
import ssl
import sys
import time
from datetime import datetime, timezone

import config
from logging_config import configure_logging

# ── Logging (Factor XI) ───────────────────────────────────────────────────────
configure_logging()
log = logging.getLogger(__name__)

# ── SSL bypass for speedtest-cli on some platforms ────────────────────────────
ssl._create_default_https_context = ssl._create_unverified_context

try:
    import speedtest as _st_mod
except ImportError:
    import subprocess
    subprocess.check_call(["uv", "add", "speedtest-cli"])  # noqa: S603
    import speedtest as _st_mod


# ── DB helpers (Factor IV) ────────────────────────────────────────────────────

def load_db() -> dict:
    if not config.DB_FILE.exists():
        log.error("DB file not found at %s. Run get-servers.py first.", config.DB_FILE)
        sys.exit(1)
    with open(config.DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def find_servers(
    db: dict,
    country_query: str | None = None,
    sponsor_query: str | None = None,
) -> list[dict]:
    """Return servers matching country name/code and/or sponsor (partial, case-insensitive)."""
    servers = list(db["servers"].values())

    if country_query:
        q = country_query.strip().lower()
        servers = [
            srv for srv in servers
            if srv.get("country", "").lower() == q
            or srv.get("cc", "").lower() == q
        ]

    if sponsor_query:
        pattern = re.compile(re.escape(sponsor_query.strip()), re.IGNORECASE)
        servers = [
            srv for srv in servers
            if pattern.search(srv.get("sponsor", ""))
        ]

    return servers


# ── Single-server speedtest ───────────────────────────────────────────────────

def run_test(srv: dict) -> dict | None:
    """Run a speedtest against one server. Returns result dict or None on error."""
    try:
        s = _st_mod.Speedtest(secure=True)
        injected = {
            "url":     srv["url"],
            "lat":     srv["lat"],
            "lon":     srv["lon"],
            "name":    srv["name"],
            "country": srv["country"],
            "cc":      srv["cc"],
            "sponsor": srv["sponsor"],
            "id":      str(srv["id"]),
            "host":    srv.get("host", ""),
            "d":       float(srv.get("distance", 0)),
        }
        best = s.get_best_server([injected])
        dl   = s.download()
        ul   = s.upload(pre_allocate=False)
        return {
            "id":            srv["id"],
            "city":          srv["name"],
            "country":       srv["country"],
            "cc":            srv["cc"],
            "sponsor":       srv["sponsor"],
            "host":          srv.get("host", ""),
            "ping_ms":       round(best.get("latency", 0), 2),
            "loss_pct":      "N/A",
            "download_mbps": round(dl / 1_000_000, 2),
            "upload_mbps":   round(ul / 1_000_000, 2),
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        log.warning("Speedtest failed for server %s (%s): %s", srv.get("id"), srv.get("name"), exc)
        return None


# ── Output formatting ─────────────────────────────────────────────────────────

def format_table(results: list[dict]) -> str:
    lines = []
    for r in results:
        city_cc = f"{r['city']}, {r['cc']}"
        lines.append(
            f"{city_cc:<22} {r['ping_ms']:<8} ms  "
            f"{r['loss_pct']:<6}  "
            f"{r['download_mbps']:<10} Mbps  "
            f"{r['upload_mbps']:<10} Mbps  "
            f"{r['sponsor']}"
        )
    return "\n".join(lines)


# ── Main (Factor XII – admin / one-off process) ───────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run speedtests against Speedtest.net servers (one-off admin task)."
    )
    parser.add_argument(
        "country",
        nargs="?",
        help="Country name or 2-letter code (e.g. Germany or DE)",
    )
    parser.add_argument(
        "--sponsor", "-s",
        default=None,
        help="Filter servers by sponsor name (partial match)",
    )
    args = parser.parse_args()

    if not args.country and not args.sponsor:
        parser.print_help()
        sys.exit(1)

    db      = load_db()
    servers = find_servers(db, country_query=args.country, sponsor_query=args.sponsor)

    if not servers:
        desc = ""
        if args.country: desc += f'country="{args.country}"'
        if args.sponsor: desc += (" " if desc else "") + f'sponsor="{args.sponsor}"'
        log.error("No servers found for %s in %s", desc, config.DB_FILE)
        if args.country:
            sample = sorted({s["country"] for s in db["servers"].values()})[:10]
            log.info("Available countries (sample): %s", sample)
        if args.sponsor:
            sponsors = sorted({s["sponsor"] for s in db["servers"].values()})
            similar  = [sp for sp in sponsors if args.sponsor.lower() in sp.lower()][:10]
            log.info("Similar sponsors: %s", similar or sponsors[:10])
        sys.exit(1)

    # derive output tag
    if args.country and args.sponsor:
        tag = f"{servers[0]['cc']}_{re.sub(r'[^\\w]+', '_', args.sponsor.strip()).strip('_')}"
    elif args.country:
        tag = servers[0]["cc"]
    else:
        tag = re.sub(r"[^\w]+", "_", args.sponsor.strip()).strip("_")  # type: ignore[arg-type]

    desc_parts = []
    if args.country: desc_parts.append(f"{servers[0]['country']} ({servers[0]['cc']})")
    if args.sponsor: desc_parts.append(f"sponsor={args.sponsor}")
    log.info("Found %d servers [%s]", len(servers), " | ".join(desc_parts))

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_txt  = config.RESULTS_DIR / f"{tag}.txt"
    out_json = config.RESULTS_DIR / f"{tag}.json"

    # resume support
    existing: dict[str, dict] = {}
    if out_json.exists():
        try:
            existing = {str(r["id"]): r for r in json.loads(out_json.read_text())}
            log.info("Resuming: %d servers already tested.", len(existing))
        except Exception as exc:
            log.warning("Could not load existing results: %s", exc)

    all_results: list[dict] = list(existing.values())
    done_ids = set(existing.keys())
    pending  = [s for s in servers if str(s["id"]) not in done_ids]
    log.info("Pending: %d servers to test", len(pending))

    header = f"{'City, CC':<22} {'Ping':<12} {'Loss':<8} {'Download':<16} {'Upload':<16} Sponsor"
    sep    = "-" * len(header)
    # Print table header to stdout for human-readable one-off output
    print(header)
    print(sep)

    for i, srv in enumerate(pending, 1):
        label = f"{srv['name']}, {srv['cc']}"
        log.info("[%d/%d] Testing %s – %s", i, len(pending), label, srv["sponsor"])

        result = run_test(srv)
        if result is None:
            print(f"  {label:<30} FAILED")
            continue

        all_results.append(result)
        print(
            f"  {label:<30} "
            f"{result['ping_ms']} ms  "
            f"{result['download_mbps']} Mbps  "
            f"{result['upload_mbps']} Mbps  "
            f"{result['sponsor']}"
        )

        # save after every server (resume support)
        out_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))

        if i < len(pending):
            time.sleep(config.SPEEDTEST_INTER_TEST_DELAY)

    # write final text table
    header_line = f"{'City, CC':<22} {'Ping':<12} {'Loss':<8} {'Download':<16} {'Upload':<16} Sponsor\n"
    header_line += "-" * 90 + "\n"
    out_txt.write_text(header_line + format_table(all_results), encoding="utf-8")

    log.info("Done. Tested %d servers.", len(all_results))
    log.info("  Table : %s", out_txt)
    log.info("  JSON  : %s", out_json)

    print(f"\n{'=' * 60}")
    print(f"Done! Tested {len(all_results)} servers.")
    print(f"  Table : {out_txt}")
    print(f"  JSON  : {out_json}")
    print(f"{'=' * 60}\n")
    print(header_line + format_table(all_results))


if __name__ == "__main__":
    main()
