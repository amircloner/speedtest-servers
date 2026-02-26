#!/usr/bin/env python3
"""
Speedtest runner for all servers of a given country.

Usage:
    uv run python speedtest_country.py "Germany"
    uv run python speedtest_country.py "United States"

Results are written to:
    results/<country_code>.txt   – human-readable table
    results/<country_code>.json  – raw JSON with all metrics

Requires: speedtest-cli  (added to pyproject.toml automatically)
"""

import json
import os
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


def find_servers(db: dict, country_query: str) -> list[dict]:
    """Return all servers matching the country name or 2-letter code."""
    q = country_query.strip().lower()
    matches = [
        srv for srv in db["servers"].values()
        if srv.get("country", "").lower() == q
        or srv.get("cc", "").lower() == q
    ]
    return matches


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
    if len(sys.argv) < 2:
        print("Usage: uv run python speedtest_country.py <country name or code>")
        print('  e.g. uv run python speedtest_country.py "Germany"')
        print('  e.g. uv run python speedtest_country.py DE')
        sys.exit(1)

    country_query = " ".join(sys.argv[1:])

    db      = load_db()
    servers = find_servers(db, country_query)

    if not servers:
        print(f'No servers found for "{country_query}" in db.json.')
        print("Available countries (sample):", sorted({s["country"] for s in db["servers"].values()})[:10])
        sys.exit(1)

    # use the actual country name and cc from the first match
    country_name = servers[0]["country"]
    country_cc   = servers[0]["cc"]
    print(f'Found {len(servers)} servers for {country_name} ({country_cc})')

    RESULTS_DIR.mkdir(exist_ok=True)
    out_txt  = RESULTS_DIR / f"{country_cc}.txt"
    out_json = RESULTS_DIR / f"{country_cc}.json"

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
