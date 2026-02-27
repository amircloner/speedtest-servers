#!/usr/bin/env python3
"""
Speedtest runner for all servers of a given country and/or sponsor.

Usage:
    uv run python speedtest_country.py "Germany"
    uv run python speedtest_country.py "United States"
    uv run python speedtest_country.py --sponsor "Melbicom"
    uv run python speedtest_country.py "Germany" --sponsor "Melbicom"

Results are written to:
    results/<country_code>.txt   – human-readable table
    results/<country_code>.json  – raw JSON with all metrics

Requires: speedtest-cli  (added to pyproject.toml automatically)
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── auto-install dependency if missing ──────────────────────────────────────
try:
    import speedtest as _st_mod
except ImportError:
    import subprocess
    subprocess.check_call(["uv", "add", "speedtest-cli"])
    import speedtest as _st_mod

# bypass macOS SSL cert verification issues
ssl._create_default_https_context = ssl._create_unverified_context

DB_FILE = Path("db.json")
RESULTS_DIR = Path("results")

# ── country name / cc lookup helpers ────────────────────────────────────────
def load_db() -> dict:
    if not DB_FILE.exists():
        print(f"Error: {DB_FILE} not found. Run get-servers.py first.")
        sys.exit(1)
    with open(DB_FILE, "r", encoding="utf-8") as f:
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


# ── single-server speedtest ──────────────────────────────────────────────────
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
            "id":        srv["id"],
            "city":      srv["name"],
            "country":   srv["country"],
            "cc":        srv["cc"],
            "sponsor":   srv["sponsor"],
            "host":      srv.get("host", ""),
            "ping_ms":   round(best.get("latency", 0), 2),
            "loss_pct":  "N/A",          # speedtest-cli doesn't measure loss
            "download_mbps": round(dl / 1_000_000, 2),
            "upload_mbps":   round(ul / 1_000_000, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"    ERROR: {e}")
        return None


# ── output formatting ────────────────────────────────────────────────────────
def format_table(results: list[dict]) -> str:
    """Format results as the requested aligned table."""
    lines = []
    for r in results:
        city_cc  = f"{r['city']}, {r['cc']}"
        ping     = f"{r['ping_ms']} ms"
        loss     = r["loss_pct"]
        dl       = f"{r['download_mbps']} Mbps"
        ul       = f"{r['upload_mbps']} Mbps"
        sponsor  = r["sponsor"]

        lines.append(
            f"{city_cc:<22} {ping:<12} {loss:<8} {dl:<16} {ul:<16} {sponsor}"
        )
    return "\n".join(lines)


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run speedtests against Speedtest.net servers."
    )
    parser.add_argument(
        "country",
        nargs="?",
        help="Country name or 2-letter code (e.g. Germany or DE)",
    )
    parser.add_argument(
        "--sponsor", "-s",
        default=None,
        help="Filter servers by sponsor name (partial match, e.g. Melbicom)",
    )
    args = parser.parse_args()

    if not args.country and not args.sponsor:
        parser.print_help()
        sys.exit(1)

    db      = load_db()
    servers = find_servers(db, country_query=args.country, sponsor_query=args.sponsor)

    if not servers:
        desc = ""
        if args.country:  desc += f'country="{args.country}"'
        if args.sponsor:  desc += (" " if desc else "") + f'sponsor="{args.sponsor}"'
        print(f'No servers found for {desc} in db.json.')
        if args.country:
            print("Available countries (sample):", sorted({s["country"] for s in db["servers"].values()})[:10])
        if args.sponsor:
            sponsors = sorted({s["sponsor"] for s in db["servers"].values()})
            sample = [sp for sp in sponsors if args.sponsor.lower() in sp.lower()][:10]
            print("Similar sponsors:", sample or sponsors[:10])
        sys.exit(1)

    # derive output file name from country cc or sponsor slug
    if args.country:
        country_cc = servers[0]["cc"]
        tag = country_cc
    else:
        tag = re.sub(r"[^\w]+", "_", args.sponsor.strip()).strip("_")

    if args.sponsor and args.country:
        tag = f"{servers[0]['cc']}_{re.sub(r'[^\w]+', '_', args.sponsor.strip()).strip('_')}"

    desc_parts = []
    if args.country:  desc_parts.append(f"{servers[0]['country']} ({servers[0]['cc']})")
    if args.sponsor:  desc_parts.append(f"sponsor={args.sponsor}")
    print(f'Found {len(servers)} servers [{" | ".join(desc_parts)}]')

    RESULTS_DIR.mkdir(exist_ok=True)
    out_txt  = RESULTS_DIR / f"{tag}.txt"
    out_json = RESULTS_DIR / f"{tag}.json"

    # load existing results so we can resume
    existing: dict[str, dict] = {}
    if out_json.exists():
        try:
            existing = {str(r["id"]): r for r in json.loads(out_json.read_text())}
            print(f"Resuming: {len(existing)} servers already tested.")
        except Exception:
            pass

    all_results: list[dict] = list(existing.values())
    done_ids = set(existing.keys())

    pending = [s for s in servers if str(s["id"]) not in done_ids]
    print(f"Pending : {len(pending)} servers to test\n")

    header = f"{'City, CC':<22} {'Ping':<12} {'Loss':<8} {'Download':<16} {'Upload':<16} Sponsor"
    sep    = "-" * len(header)
    print(header)
    print(sep)

    for i, srv in enumerate(pending, 1):
        label = f"{srv['name']}, {srv['cc']}"
        print(f"[{i}/{len(pending)}] {label:<30} ... ", end="", flush=True)

        result = run_test(srv)
        if result is None:
            print("FAILED")
            continue

        all_results.append(result)

        row = (
            f"{result['ping_ms']} ms  "
            f"{result['loss_pct']}  "
            f"{result['download_mbps']} Mbps  "
            f"{result['upload_mbps']} Mbps  "
            f"{result['sponsor']}"
        )
        print(row)

        # save after every server (resume support)
        out_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))

        # small pause to avoid being rate-limited
        if i < len(pending):
            time.sleep(1)

    # write final text table
    table = format_table(all_results)
    header_line = f"{'City, CC':<22} {'Ping':<12} {'Loss':<8} {'Download':<16} {'Upload':<16} Sponsor\n"
    header_line += "-" * 90 + "\n"
    out_txt.write_text(header_line + table, encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"Done! Tested {len(all_results)} servers.")
    print(f"  Table : {out_txt}")
    print(f"  JSON  : {out_json}")
    print(f"{'=' * 60}\n")
    print(header_line + table)


if __name__ == "__main__":
    main()
