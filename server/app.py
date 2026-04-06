"""
CAM PLAYS Dashboard Server
Receives TradingView webhook alerts and serves a real-time dashboard.

Supports two alert formats:
1. JSON: {"ticker":"ES1!","event":"entry","play":"HA","time":"..."}
2. Text:  "HA entry event on ES1! at 09:42:00"
"""

import os
import re
import json
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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
            "candidates": [], "passed": [],
            "range": None, "width": None, "dataSource": None, "lastUpdate": None,
        }
    return instruments[ticker]


def reset_session(ticker):
    inst = get_or_create_instrument(ticker)
    inst.update({"active": None, "activeDirection": None, "activeDesc": None,
                 "candidates": [], "passed": [], "range": None, "width": None,
                 "dataSource": None, "lastUpdate": datetime.now(timezone.utc).isoformat()})


def process_entry(ticker, play, alert_time):
    if play not in PLAY_META:
        return {"error": f"Unknown play: {play}"}, 400
    inst = get_or_create_instrument(ticker)
    inst["lastUpdate"] = alert_time or datetime.now(timezone.utc).isoformat()
    if inst["active"] and inst["active"] != play:
        if inst["active"] not in inst["passed"]:
            inst["passed"].append(inst["active"])
    meta = PLAY_META[play]
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

    ticker = data.get("ticker", "").strip()
    event = data.get("event", "").strip().lower()
    play = data.get("play", "").strip().upper()
    alert_time = data.get("time", "")
    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    inst = get_or_create_instrument(ticker)
    inst["lastUpdate"] = alert_time or datetime.now(timezone.utc).isoformat()

    if event == "entry":
        result, code = process_entry(ticker, play, alert_time)
        return jsonify(result), code
    elif event == "candidate":
        if play in PLAY_META and play not in inst["candidates"] and play != inst["active"]:
            inst["candidates"].append(play)
    elif event == "candidate_remove":
        if play in inst["candidates"]:
            inst["candidates"].remove(play)
    elif event == "exit":
        if inst["active"] == play:
            if play not in inst["passed"]:
                inst["passed"].append(play)
            inst["active"] = None
            inst["activeDirection"] = None
            inst["activeDesc"] = None
    elif event == "context":
        for k in ("range", "width", "dataSource"):
            if k in data:
                inst[k] = data[k]
    elif event == "session_reset":
        reset_session(ticker)
    else:
        return jsonify({"error": f"Unknown event: {event}"}), 400
    return jsonify({"status": "ok", "ticker": ticker, "event": event})


@app.route("/api/state")
def get_state():
    result = sorted(instruments.values(), key=lambda x: x["ticker"])
    return jsonify({"instruments": result, "serverTime": datetime.now(timezone.utc).isoformat()})

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
