"""
CAM PLAYS Dashboard Server
Receives TradingView webhook alerts and serves a real-time dashboard.

Supports three alert formats:
1. EZCam V12 (one message per bar close, events + full snapshot):
   {"source":"EZCam V12","v":1,"ticker":"MES1!","time":"...",
    "events":[{"event":"entry","play":"HA"}, ...],
    "snapshot":{"range":"Higher Range","width":"Wide pivots","dataSource":"Daily","price":5858.25,"dayHigh":5862.0,"dayLow":5832.0,
                "levels":{"S6":..,"S5":..,"S4":..,"S3":..,"CP":..,"R3":..,"R4":..,"R5":..,"R6":..},
                "active":"HA"|null,"candidates":["HB"],"passed":["LA"]}}
2. Legacy JSON (EZ Cam Dashboard V3): {"ticker":"ES1!","event":"entry","play":"HA","time":"..."}
3. Legacy text:  "HA entry event on ES1! at 09:42:00"

Play-start log: every play start (entry event) is logged with the server time, kept 36 hours and saved to the
Supabase table play_starts (ticker, play, started_at) so it survives restarts. /api/state returns it as "log"
(newest first); the page shows the current session and counts the new active plays on its bell.

Manual Bias (set by the user on the page, ticker x W1/D1/H1/M15 = none/up/down) is kept in memory and
saved to Supabase so it survives restarts and redeploys. Render environment settings:
  SUPABASE_URL         https://<project>.supabase.co
  SUPABASE_SECRET_KEY  the project's secret key (sb_secret_...) or legacy service_role key
  BIAS_PIN             PIN needed to change Bias (without it, Bias is read-only)
Without the two Supabase settings Bias still works but is lost on restart.
"""

import os
import re
import json
import hmac
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Bias-Pin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

instruments = {}

INSTRUMENT_DEFAULTS = {
    "MES1!": {"exchange": "CME_MINI",  "name": "Micro S&P 500"},
    "MNQ1!": {"exchange": "CME_MINI",  "name": "Micro Nasdaq 100"},
    "MYM1!": {"exchange": "CBOT_MINI", "name": "Micro Dow Jones"},
    "M2K1!": {"exchange": "CME_MINI",  "name": "Micro Russell 2000"},
    "MGC1!": {"exchange": "COMEX",     "name": "Micro Gold"},
    "SIL1!": {"exchange": "COMEX",     "name": "Silver"},
    "MCL1!": {"exchange": "NYMEX",     "name": "Micro Crude Oil"},
    "MBT1!": {"exchange": "CME",       "name": "Micro Bitcoin"},
}

PLAY_META = {
    "HA": {"direction": "long",  "desc": "S3 → R3"},
    "HB": {"direction": "long",  "desc": "R4 → R6"},
    "HC": {"direction": "long",  "desc": "R4 → R6"},
    "HD": {"direction": "short", "desc": "S4 → S6"},
    "HE": {"direction": "short", "desc": "R4 → S4"},
    "HF": {"direction": "short", "desc": "R6 → CP"},
    "LA": {"direction": "short", "desc": "R3 → S3"},
    "LB": {"direction": "short", "desc": "S4 → S6"},
    "LC": {"direction": "short", "desc": "S4 → S6"},
    "LD": {"direction": "long",  "desc": "R4 → R6"},
    "LE": {"direction": "long",  "desc": "S4 → R4"},
    "LF": {"direction": "long",  "desc": "S6 → CP"},
}

TEXT_ALERT_PATTERN = re.compile(
    r'^(\w{2})\s+entry\s+event\s+on\s+(.+?)\s+at\s+(.+)$',
    re.IGNORECASE
)


def get_or_create_instrument(ticker):
    if ticker not in instruments:
        defaults = INSTRUMENT_DEFAULTS.get(ticker, {"exchange": "", "name": ticker})
        instruments[ticker] = {
            "ticker": ticker, "exchange": defaults["exchange"], "name": defaults["name"],
            "active": None, "activeDirection": None, "activeDesc": None,
            # Server time (UTC) the active play started, from its entry event (or, after a restart, from the
            # play-start log); None = not known. The page uses it to tell which active plays are new.
            "activeSince": None,
            "candidates": [], "passed": [],
            "range": None, "width": None, "dataSource": None, "lastUpdate": None,
            "price": None, "dayHigh": None, "dayLow": None, "levels": None, "source": None,
            "receivedAt": None,  # server time (UTC) of the last webhook for this ticker: the dashboard's feed light
            # Plays that ended this session, oldest first: {"play", "result": "target"|"failed"|None (V3), "time"}.
            # The snapshot's "passed" list doesn't say how a play ended; the exit events do.
            "exits": [],
        }
    return instruments[ticker]


def reset_session(ticker):
    inst = get_or_create_instrument(ticker)
    inst.update({"active": None, "activeDirection": None, "activeDesc": None, "activeSince": None,
                 "candidates": [], "passed": [], "range": None, "width": None,
                 "dataSource": None, "price": None, "dayHigh": None, "dayLow": None, "levels": None,
                 "exits": [],
                 "lastUpdate": datetime.now(timezone.utc).isoformat()})


def process_entry(ticker, play, alert_time):
    if play not in PLAY_META:
        return {"error": f"Unknown play: {play}"}, 400
    inst = get_or_create_instrument(ticker)
    inst["lastUpdate"] = alert_time or datetime.now(timezone.utc).isoformat()
    if inst["active"] and inst["active"] != play:
        if inst["active"] not in inst["passed"]:
            inst["passed"].append(inst["active"])
    meta = PLAY_META[play]
    if inst["active"] != play:
        started = datetime.now(timezone.utc).isoformat()
        inst["activeSince"] = started
        log_start(ticker, play, started)
    inst["active"] = play
    inst["activeDirection"] = meta["direction"]
    inst["activeDesc"] = meta["desc"]
    if play in inst["candidates"]:
        inst["candidates"].remove(play)
    app.logger.info(f"ENTRY: {ticker} {play} at {alert_time}")
    return {"status": "ok", "ticker": ticker, "event": "entry", "play": play}, 200


@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return "", 200
    raw = request.get_data(as_text=True).strip()
    data = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    if data is None:
        match = TEXT_ALERT_PATTERN.match(raw)
        if match:
            result, code = process_entry(match.group(2).strip(), match.group(1).upper(), match.group(3).strip())
            return jsonify(result), code
        return jsonify({"error": "Could not parse alert"}), 400

    ticker = str(data.get("ticker", "")).strip()
    alert_time = data.get("time", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    inst = get_or_create_instrument(ticker)
    inst["lastUpdate"] = alert_time or datetime.now(timezone.utc).isoformat()
    inst["receivedAt"] = datetime.now(timezone.utc).isoformat()
    bias_keepalive()

    # EZCam V12: several events plus a snapshot in one message
    if "events" in data or "snapshot" in data:
        inst["source"] = data.get("source")
        events = data.get("events") or []
        for ev in events:
            if isinstance(ev, dict):
                apply_event(ticker, str(ev.get("event", "")).strip().lower(),
                            str(ev.get("play", "") or "").strip().upper(), ev, alert_time)
        if isinstance(data.get("snapshot"), dict):
            apply_snapshot(ticker, data["snapshot"])
        return jsonify({"status": "ok", "ticker": ticker, "events": len(events),
                        "snapshot": isinstance(data.get("snapshot"), dict)})

    # Legacy V3: one event per message
    event = str(data.get("event", "")).strip().lower()
    play = str(data.get("play", "") or "").strip().upper()
    result, code = apply_event(ticker, event, play, data, alert_time)
    return jsonify(result), code


def apply_snapshot(ticker, snap):
    """Replace the ticker's state with the indicator's full snapshot (source of truth)."""
    inst = get_or_create_instrument(ticker)
    for k in ("range", "width", "dataSource", "price", "dayHigh", "dayLow"):
        if k in snap:
            inst[k] = snap[k]
    if isinstance(snap.get("levels"), dict):
        inst["levels"] = {k: v for k, v in snap["levels"].items() if isinstance(v, (int, float))}
    active = snap.get("active")
    active = active.upper() if isinstance(active, str) and active.upper() in PLAY_META else None
    if active != inst["active"]:
        # changed without an entry event (e.g. the server restarted mid-play): take the start time from the log
        inst["activeSince"] = log_last_start(ticker, active) if active else None
    inst["active"] = active
    inst["activeDirection"] = PLAY_META[active]["direction"] if active else None
    inst["activeDesc"] = PLAY_META[active]["desc"] if active else None
    def plays(key):
        return [p.upper() for p in (snap.get(key) or []) if isinstance(p, str) and p.upper() in PLAY_META]
    inst["candidates"] = [p for p in plays("candidates") if p != active]
    inst["passed"] = plays("passed")


def apply_event(ticker, event, play, data, alert_time):
    """Apply one event. Returns (result, http_code)."""
    inst = get_or_create_instrument(ticker)
    if event == "entry":
        return process_entry(ticker, play, alert_time)
    elif event == "candidate":
        if play in PLAY_META and play not in inst["candidates"] and play != inst["active"]:
            inst["candidates"].append(play)
    elif event == "candidate_remove":
        if play in inst["candidates"]:
            inst["candidates"].remove(play)
    elif event == "exit":
        if play in PLAY_META:
            result = str(data.get("result", "") or "").strip().lower()
            inst["exits"] = (inst.get("exits") or [])[-19:] + [{
                "play": play, "result": result if result in ("target", "failed") else None,
                "time": alert_time or datetime.now(timezone.utc).isoformat()}]
        if inst["active"] == play:
            if play not in inst["passed"]:
                inst["passed"].append(play)
            inst["active"] = None
            inst["activeSince"] = None
            inst["activeDirection"] = None
            inst["activeDesc"] = None
    elif event == "context":
        for k in ("range", "width", "dataSource"):
            if k in data:
                inst[k] = data[k]
    elif event == "session_reset":
        reset_session(ticker)
        inst["lastUpdate"] = alert_time or inst["lastUpdate"]
    else:
        return {"error": f"Unknown event: {event}"}, 400
    return {"status": "ok", "ticker": ticker, "event": event}, 200


# ---------------- Play-start log ----------------
LOG_KEEP_S = 36 * 3600          # the page shows the current session (since 18:00 ET); 36 h always covers it
play_log = []                  # [{"time": iso UTC, "ticker", "play"}], newest first
log_lock = threading.Lock()
log_db = {"ok": False, "error": None, "lastTry": 0.0, "lastPrune": 0.0}


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _log_merge(entries):
    """Add entries (deduplicated), keep 36 hours, newest first. Call with log_lock held."""
    seen = {(e["ticker"], e["play"], _ts(e["time"])) for e in play_log}
    for e in entries:
        k = (e["ticker"], e["play"], _ts(e["time"]))
        if k not in seen:
            seen.add(k)
            play_log.append(e)
    cutoff = time.time() - LOG_KEEP_S
    play_log[:] = sorted((e for e in play_log if _ts(e["time"]) >= cutoff), key=lambda e: _ts(e["time"]), reverse=True)[:300]


def log_start(ticker, play, started):
    """Record a play start in memory and, in the background, in Supabase."""
    with log_lock:
        _log_merge([{"time": started, "ticker": ticker, "play": play}])
    if not db_configured():
        return
    def save():
        try:
            sb_request("POST", "play_starts", {"ticker": ticker, "play": play, "started_at": started}, prefer="return=minimal")
            if time.time() - log_db["lastPrune"] > 86400:    # once a day: drop rows older than 3 days
                log_db["lastPrune"] = time.time()
                old = datetime.fromtimestamp(time.time() - 3 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                sb_request("DELETE", "play_starts?started_at=lt." + old)
        except Exception as e:
            app.logger.error(f"Play-start save failed: {e}")
            log_db.update(ok=False, error="Could not save play starts to the database")
    threading.Thread(target=save, daemon=True).start()


def log_last_start(ticker, play):
    """Start time of the latest logged start of this play on this ticker, or None."""
    with log_lock:
        for e in play_log:
            if e["ticker"] == ticker and e["play"] == play:
                return e["time"]
    return None


def load_log():
    """Load the last 36 hours of play starts from Supabase (at startup, then retried after a failure)."""
    log_db["lastTry"] = time.time()
    if not db_configured():
        log_db.update(ok=False, error="No database set up: the play-start log is lost when the server restarts")
        return
    try:
        since = datetime.fromtimestamp(time.time() - LOG_KEEP_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = sb_request("GET", "play_starts?select=ticker,play,started_at&started_at=gte." + urllib.parse.quote(since)
                          + "&order=started_at.desc&limit=300")
        entries = [{"time": r["started_at"], "ticker": r["ticker"], "play": r["play"]}
                   for r in rows or [] if r.get("play") in PLAY_META and r.get("ticker") and r.get("started_at")]
        with log_lock:
            _log_merge(entries)
        # plays already active (seen before the log loaded) get their start time back
        for inst in instruments.values():
            if inst["active"] and not inst.get("activeSince"):
                inst["activeSince"] = log_last_start(inst["ticker"], inst["active"])
        log_db.update(ok=True, error=None)
    except Exception as e:
        app.logger.error(f"Play-start log load failed: {e}")
        log_db.update(ok=False, error="Database unreachable: the play-start log only has starts since the server started")


def log_retry_load():
    if db_configured() and not log_db["ok"] and time.time() - log_db["lastTry"] > 60:
        threading.Thread(target=load_log, daemon=True).start()


@app.route("/api/state")
def get_state():
    result = sorted(instruments.values(), key=lambda x: x["ticker"])
    bias_retry_load()
    log_retry_load()
    with log_lock:
        log = list(play_log)
    with bias_lock:
        b = json.loads(json.dumps(bias))
    return jsonify({"instruments": result, "serverTime": datetime.now(timezone.utc).isoformat(),
                    "bias": b, "biasInfo": {"db": bias_db["ok"], "error": bias_db["error"], "editable": bool(BIAS_PIN)},
                    "log": log, "logInfo": {"db": log_db["ok"], "error": log_db["error"]}})


# ---------------- Manual Bias ----------------
BIAS_TFS = ("W1", "D1", "H1", "M15")
BIAS_VALUES = ("none", "up", "down")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
BIAS_PIN = os.environ.get("BIAS_PIN", "").strip()
bias = {}                      # {ticker: {tf: {"v": "up"|"down"|"none", "t": iso time set, or None = never set}}}
bias_lock = threading.Lock()
bias_db = {"ok": False, "error": None, "lastTry": 0.0, "lastTouch": 0.0}
pin_fails = {}                 # {ip: [failure times]}: slows down PIN guessing


def db_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_request(method, path, body=None, prefer=None):
    """Call Supabase's REST API (PostgREST) with the secret key. Raises on any HTTP or network error."""
    headers = {"apikey": SUPABASE_KEY, "Accept": "application/json", "Content-Type": "application/json"}
    if SUPABASE_KEY.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
        headers["Authorization"] = "Bearer " + SUPABASE_KEY
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read()
    bias_db["lastTouch"] = time.time()
    return json.loads(raw) if raw else None


def load_bias():
    global bias
    blank = {t: {tf: {"v": "none", "t": None} for tf in BIAS_TFS} for t in INSTRUMENT_DEFAULTS}
    bias_db["lastTry"] = time.time()
    if not db_configured():
        with bias_lock:
            bias = blank
        bias_db.update(ok=False, error="No database set up: Bias changes are lost when the server restarts")
        return
    try:
        rows = sb_request("GET", "bias?select=ticker,tf,value,set_at")
        for r in rows or []:
            if r.get("ticker") in blank and r.get("tf") in BIAS_TFS and r.get("value") in BIAS_VALUES:
                blank[r["ticker"]][r["tf"]] = {"v": r["value"], "t": r.get("set_at")}
        with bias_lock:
            bias = blank
        bias_db.update(ok=True, error=None)
    except Exception as e:
        app.logger.error(f"Bias load failed: {e}")
        if not bias:
            with bias_lock:
                bias = blank
        bias_db.update(ok=False, error="Database unreachable: showing Bias without saved values, retrying")


def bias_retry_load():
    """After a failed load (e.g. Supabase paused or down), try again at most once a minute."""
    if db_configured() and not bias_db["ok"] and time.time() - bias_db["lastTry"] > 60:
        load_bias()


def bias_keepalive():
    """Supabase pauses free projects after a week without requests: touch it once a day (in the background)."""
    if db_configured() and bias_db["ok"] and time.time() - bias_db["lastTouch"] > 86400:
        bias_db["lastTouch"] = time.time()
        def touch():
            try:
                sb_request("GET", "bias?select=ticker&limit=1")
            except Exception as e:
                app.logger.warning(f"Supabase keep-alive failed: {e}")
        threading.Thread(target=touch, daemon=True).start()


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")


def check_pin(pin):
    """(ok, error, http_code). At most 5 wrong PINs per 15 minutes per address."""
    if not BIAS_PIN:
        return False, "Bias is read-only: BIAS_PIN is not set on the server", 403
    ip, now = client_ip(), time.time()
    fails = [t for t in pin_fails.get(ip, []) if now - t < 900]
    pin_fails[ip] = fails
    if len(fails) >= 5:
        return False, "Too many wrong PINs, try again in 15 minutes", 429
    if hmac.compare_digest(str(pin or "").encode(), BIAS_PIN.encode()):
        return True, None, 200
    fails.append(now)
    return False, "Wrong PIN", 401


@app.route("/api/bias/pin", methods=["POST"])
def bias_pin():
    ok, err, code = check_pin(request.headers.get("X-Bias-Pin"))
    return jsonify({"ok": ok, "error": err}), code


@app.route("/api/bias", methods=["POST"])
def bias_set():
    """Body: {"changes": [{"ticker": "MES1!", "tf": "H1", "v": "up"|"down"|"none", "clear": false}, ...]}
    clear=true resets the cell to 'never set'. Saved to the database first; memory only changes if that worked."""
    ok, err, code = check_pin(request.headers.get("X-Bias-Pin"))
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    data = request.get_json(silent=True) or {}
    changes = data.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 64:
        return jsonify({"ok": False, "error": "No changes"}), 400
    now = datetime.now(timezone.utc).isoformat()
    rows = {}
    for c in changes:
        if not isinstance(c, dict):
            return jsonify({"ok": False, "error": "Bad change"}), 400
        t, tf = c.get("ticker"), c.get("tf")
        clear = bool(c.get("clear"))
        v = "none" if clear else c.get("v")
        if t not in INSTRUMENT_DEFAULTS or tf not in BIAS_TFS or v not in BIAS_VALUES:
            return jsonify({"ok": False, "error": f"Bad change: {t} {tf} {v}"}), 400
        rows[(t, tf)] = {"ticker": t, "tf": tf, "value": v, "set_at": None if clear else now}
    if db_configured():
        try:
            sb_request("POST", "bias?on_conflict=ticker,tf", list(rows.values()),
                       prefer="resolution=merge-duplicates,return=minimal")
            bias_db.update(ok=True, error=None)
        except Exception as e:
            app.logger.error(f"Bias save failed: {e}")
            return jsonify({"ok": False, "error": "Could not save to the database"}), 502
    with bias_lock:
        for r in rows.values():
            bias.setdefault(r["ticker"], {})[r["tf"]] = {"v": r["value"], "t": r["set_at"]}
    return jsonify({"ok": True, "saved": db_configured(), "time": now})

@app.route("/api/instruments", methods=["POST"])
def add_instrument():
    data = request.get_json(force=True)
    ticker = data.get("ticker", "").strip()
    if ticker and ticker not in instruments:
        get_or_create_instrument(ticker)
    return jsonify({"status": "ok", "ticker": ticker})

@app.route("/api/reset", methods=["POST"])
def reset_all():
    for ticker in instruments:
        reset_session(ticker)
    return jsonify({"status": "ok"})

@app.route("/")
def serve_dashboard():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route("/health")
def health():
    return jsonify({"status": "healthy", "instruments": len(instruments)})

def init_defaults():
    for ticker in INSTRUMENT_DEFAULTS:
        get_or_create_instrument(ticker)

init_defaults()
load_bias()
load_log()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
