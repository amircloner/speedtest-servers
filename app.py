#!/usr/bin/env python3
"""
Flask web GUI for speedtest_country.py
Allows selecting country and sponsor via dropdowns,
runs speedtest, and displays results in real-time via SSE.
"""

import json
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

from flask import Flask, Response, jsonify, render_template, request

# bypass SSL cert verification issues
ssl._create_default_https_context = ssl._create_unverified_context

try:
    import speedtest as _st_mod
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "speedtest-cli"])
    import speedtest as _st_mod

app = Flask(__name__)

DB_FILE = Path("db.json")
RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)

# Active SSE streams: job_id -> Queue
_streams: dict[str, Queue] = {}
_stream_lock = threading.Lock()


def load_db() -> dict:
    if not DB_FILE.exists():
        return {"servers": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_countries(db: dict) -> list[str]:
    countries = sorted(set(
        s.get("country", "") for s in db["servers"].values() if s.get("country")
    ))
    return countries


def get_sponsors_for_country(db: dict, country: str) -> list[str]:
    sponsors = sorted(set(
        s.get("sponsor", "")
        for s in db["servers"].values()
        if s.get("country", "").lower() == country.lower() and s.get("sponsor")
    ))
    return sponsors


def find_servers(db: dict, country: str | None, sponsor: str | None) -> list[dict]:
    servers = list(db["servers"].values())
    if country:
        q = country.strip().lower()
        servers = [
            s for s in servers
            if s.get("country", "").lower() == q or s.get("cc", "").lower() == q
        ]
    if sponsor:
        pattern = re.compile(re.escape(sponsor.strip()), re.IGNORECASE)
        servers = [s for s in servers if pattern.search(s.get("sponsor", ""))]
    return servers


def run_single_test(srv: dict) -> dict | None:
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
            "id":             srv["id"],
            "city":           srv["name"],
            "country":        srv["country"],
            "cc":             srv["cc"],
            "sponsor":        srv["sponsor"],
            "host":           srv.get("host", ""),
            "ping_ms":        round(best.get("latency", 0), 2),
            "loss_pct":       "N/A",
            "download_mbps":  round(dl / 1_000_000, 2),
            "upload_mbps":    round(ul / 1_000_000, 2),
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def run_speedtest_job(job_id: str, country: str, sponsor: str):
    """Run speedtest in background thread and push results via queue."""
    q = _streams.get(job_id)
    if q is None:
        return

    def send(event_type: str, data: dict):
        q.put({"event": event_type, "data": data})

    try:
        db = load_db()
        servers = find_servers(db, country or None, sponsor or None)

        if not servers:
            send("error", {"message": f"No servers found for country='{country}' sponsor='{sponsor}'"})
            send("done", {"total": 0})
            return

        send("info", {"message": f"Found {len(servers)} servers. Starting tests...", "total": len(servers)})

        all_results: list[dict] = []

        for i, srv in enumerate(servers, 1):
            send("progress", {
                "current": i,
                "total": len(servers),
                "server": f"{srv['name']}, {srv['cc']} — {srv['sponsor']}",
            })

            result = run_single_test(srv)

            if result and "error" not in result:
                all_results.append(result)
                send("result", result)
            else:
                send("failed", {
                    "server": f"{srv['name']}, {srv['cc']}",
                    "error": result.get("error", "Unknown error") if result else "Unknown error",
                })

            if i < len(servers):
                time.sleep(1)

        # save results
        if all_results:
            tag = re.sub(r"[^\w]+", "_", (country or sponsor or "results").strip()).strip("_")
            out_json = RESULTS_DIR / f"{tag}.json"
            out_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
            send("saved", {"file": str(out_json)})

        send("done", {"total": len(all_results)})

    except Exception as e:
        send("error", {"message": str(e)})
        send("done", {"total": 0})
    finally:
        # clean up queue after 5 min
        def cleanup():
            time.sleep(300)
            with _stream_lock:
                _streams.pop(job_id, None)
        threading.Thread(target=cleanup, daemon=True).start()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    db = load_db()
    countries = get_all_countries(db)
    return render_template("index.html", countries=countries)


@app.route("/api/sponsors")
def api_sponsors():
    country = request.args.get("country", "")
    db = load_db()
    sponsors = get_sponsors_for_country(db, country)
    return jsonify(sponsors)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    country = data.get("country", "").strip()
    sponsor = data.get("sponsor", "").strip()

    if not country and not sponsor:
        return jsonify({"error": "Select at least a country or sponsor"}), 400

    job_id = str(uuid.uuid4())
    q: Queue = Queue()
    with _stream_lock:
        _streams[job_id] = q

    thread = threading.Thread(
        target=run_speedtest_job,
        args=(job_id, country, sponsor),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def api_stream(job_id: str):
    q = _streams.get(job_id)
    if q is None:
        return Response("Job not found", status=404)

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                event = msg["event"]
                data = json.dumps(msg["data"])
                yield f"event: {event}\ndata: {data}\n\n"
                if event == "done":
                    break
            except Empty:
                yield "event: ping\ndata: {}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/results")
def api_results():
    results = []
    for f in RESULTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            results.append({"file": f.name, "count": len(data), "data": data})
        except Exception:
            pass
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
