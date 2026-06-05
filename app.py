#!/usr/bin/env python3
"""
Flask web GUI for speedtest_country.py

12-Factor compliance:
  III  – Config read from environment (via config.py)
  IV   – DB_FILE / RESULTS_DIR treated as backing-service URLs
  VI   – Stateless process: no in-process shared mutable state beyond
         the ephemeral job queues that are intentionally short-lived
  VII  – Port binding driven by APP_HOST / APP_PORT env vars
  IX   – Graceful shutdown handled by gunicorn (SIGTERM → drain workers)
  XI   – All logging goes to stdout via logging_config.py
"""

import json
import logging
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from queue import Empty, Queue

from flask import Flask, Response, jsonify, render_template, request

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
    subprocess.check_call(["pip", "install", "speedtest-cli"])  # noqa: S603
    import speedtest as _st_mod

# ── Application factory (Factor VI – stateless) ───────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

# ── Ensure results directory exists (Factor IV – backing service path) ─────────
config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Active SSE streams: job_id → Queue ───────────────────────────────────────
# These queues are ephemeral and local to this process instance.
# For multi-process deployments a Redis pub/sub backing service would replace this.
_streams: dict[str, Queue] = {}
_stream_lock = threading.Lock()


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_db() -> dict:
    """Load server database from the configured backing-service path."""
    if not config.DB_FILE.exists():
        log.warning("DB file not found at %s – returning empty database", config.DB_FILE)
        return {"servers": {}}
    with open(config.DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_countries(db: dict) -> list[str]:
    return sorted(set(
        s.get("country", "") for s in db["servers"].values() if s.get("country")
    ))


def get_sponsors_for_country(db: dict, country: str) -> list[str]:
    return sorted(set(
        s.get("sponsor", "")
        for s in db["servers"].values()
        if s.get("country", "").lower() == country.lower() and s.get("sponsor")
    ))


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


# ── Speedtest logic ────────────────────────────────────────────────────────────

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
        log.warning("Speedtest failed for server %s: %s", srv.get("id"), exc)
        return {"error": str(exc)}


def run_speedtest_job(job_id: str, country: str, sponsor: str) -> None:
    """Run speedtest in a background thread; push results to the SSE queue."""
    q = _streams.get(job_id)
    if q is None:
        log.error("Job %s: queue not found", job_id)
        return

    def send(event_type: str, data: dict) -> None:
        q.put({"event": event_type, "data": data})

    log.info("Job %s started (country=%r, sponsor=%r)", job_id, country, sponsor)

    try:
        db = load_db()
        servers = find_servers(db, country or None, sponsor or None)

        if not servers:
            send("error", {"message": f"No servers found for country='{country}' sponsor='{sponsor}'"})
            send("done", {"total": 0})
            return

        send("info", {"message": f"Found {len(servers)} servers. Starting tests…", "total": len(servers)})

        all_results: list[dict] = []

        for i, srv in enumerate(servers, 1):
            send("progress", {
                "current": i,
                "total":   len(servers),
                "server":  f"{srv['name']}, {srv['cc']} — {srv['sponsor']}",
            })

            result = run_single_test(srv)

            if result and "error" not in result:
                all_results.append(result)
                send("result", result)
            else:
                send("failed", {
                    "server": f"{srv['name']}, {srv['cc']}",
                    "error":  result.get("error", "Unknown error") if result else "Unknown error",
                })

            if i < len(servers):
                time.sleep(config.SPEEDTEST_INTER_TEST_DELAY)

        # persist results (Factor IV – results dir as backing service)
        if all_results:
            tag = re.sub(r"[^\w]+", "_", (country or sponsor or "results").strip()).strip("_")
            out_json = config.RESULTS_DIR / f"{tag}.json"
            out_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
            log.info("Job %s: results saved to %s", job_id, out_json)
            send("saved", {"file": str(out_json)})

        log.info("Job %s finished – %d results", job_id, len(all_results))
        send("done", {"total": len(all_results)})

    except Exception as exc:
        log.exception("Job %s crashed: %s", job_id, exc)
        send("error", {"message": str(exc)})
        send("done", {"total": 0})
    finally:
        # Factor IX – clean up ephemeral state after TTL
        def _cleanup() -> None:
            time.sleep(config.STREAM_TTL)
            with _stream_lock:
                _streams.pop(job_id, None)
            log.debug("Job %s: queue cleaned up", job_id)

        threading.Thread(target=_cleanup, daemon=True, name=f"cleanup-{job_id}").start()


# ── Routes ─────────────────────────────────────────────────────────────────────

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

    threading.Thread(
        target=run_speedtest_job,
        args=(job_id, country, sponsor),
        daemon=True,
        name=f"job-{job_id[:8]}",
    ).start()

    log.info("Started job %s", job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def api_stream(job_id: str):
    q = _streams.get(job_id)
    if q is None:
        return Response("Job not found", status=404)

    def generate():
        while True:
            try:
                msg = q.get(timeout=config.STREAM_POLL_TIMEOUT)
                event = msg["event"]
                data  = json.dumps(msg["data"])
                yield f"event: {event}\ndata: {data}\n\n"
                if event == "done":
                    break
            except Empty:
                # keepalive – prevents proxies from closing the connection
                yield "event: ping\ndata: {}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/results")
def api_results():
    results = []
    for f in config.RESULTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            results.append({"file": f.name, "count": len(data), "data": data})
        except Exception as exc:
            log.warning("Could not read result file %s: %s", f, exc)
    return jsonify(results)


@app.route("/healthz")
def healthz():
    """Factor IX – liveness probe endpoint for container orchestrators."""
    return jsonify({"status": "ok"})


# ── Entry point (dev only – Factor VII) ───────────────────────────────────────
if __name__ == "__main__":
    # Use gunicorn in production; this branch is for local dev only.
    app.run(host=config.APP_HOST, port=config.APP_PORT, debug=config.DEBUG)
