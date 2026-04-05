"""
CAM PLAYS Dashboard Server
Receives TradingView webhook alerts and serves a real-time dashboard.

Webhook JSON format (from BruzX indicator):
{
    "ticker": "ES1!",
    "event": "entry",        # entry | candidate | exit
    "play": "HA",
    "time": "2026-04-07 09:42:00"
}
"""

import os
import json
import time as time_module
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory
try:
    from flask_cors import CORS
except ImportError:
    CORS = None

app = Flask(__name__, static_folder="static")
if CORS:
    CORS(app)
else:
    @app.after_request
    def add_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

# ---- In-memory state store ----
# Structure: { "ES1!": { "ticker", "exchange", "active", "candidates", "passed", "range", "width", "dataSource", "lastUpdate" } }
instruments = {}

# Default instrument config — maps tickers to their TradingView exchange prefix
# Users can add/remove tickers via API
INSTRUMENT_DEFAULTS = {
    "ES1!":  {"exchange": "CME_MINI",  "name": "S&P 500 E-mini"},
    "MES1!": {"exchange": "CME_MINI",  "name": "Micro S&P 500"},
    "NQ1!":  {"exchange": "CME_MINI",  "name": "Nasdaq 100 E-mini"},
    "MNQ1!": {"exchange": "CME_MINI",  "name": "Micro Nasdaq 100"},
    "YM1!":  {"exchange": "CBOT_MINI", "name": "Dow Jones E-mini"},
    "RTY1!": {"exchange": "CME_MINI",  "name": "Russell 2000 E-mini"},
    "GC1!":  {"exchange": "COMEX",     "name": "Gold"},
    "MGC1!": {"exchange": "COMEX",     "name": "Micro Gold"},
    "CL1!":  {"exchange": "NYMEX",     "name": "Crude Oil"},
    "MCL1!": {"exchange": "NYMEX",     "name": "Micro Crude Oil"},
}


def get_or_create_instrument(ticker):
    """Get instrument state, creating default if it doesn't exist."""
    if ticker not in instruments:
        defaults = INSTRUMENT_DEFAULTS.get(ticker, {"exchange": "", "name": ticker})
        instruments[ticker] = {
            "ticker": ticker,
            "exchange": defaults["exchange"],
            "name": defaults["name"],
            "active": None,
            "activeDirection": None,
            "activeDesc": None,
            "candidates": [],
            "passed": [],
            "range": None,
            "width": None,
            "dataSource": None,
            "lastUpdate": None,
        }
    return instruments[ticker]


def reset_session(ticker):
    """Reset all play states for a new trading session."""
    inst = get_or_create_instrument(ticker)
    inst["active"] = None
    inst["activeDirection"] = None
    inst["activeDesc"] = None
    inst["candidates"] = []
    inst["passed"] = []
    inst["range"] = None
    inst["width"] = None
    inst["dataSource"] = None
    inst["lastUpdate"] = datetime.now(timezone.utc).isoformat()


# Play metadata — direction and description for each play
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


# ---- Webhook endpoint ----

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receive TradingView webhook alerts.

    Expected JSON body:
    {
        "ticker": "ES1!",
        "event": "entry" | "candidate" | "exit" | "context",
        "play": "HA",
        "time": "2026-04-07 09:42:00",
        // For context events:
        "range": "Higher" | "Lower" | "Neutral",
        "width": "Wide" | "Narrow" | "Similar",
        "dataSource": "RTH" | "ETH"
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    ticker = data.get("ticker", "").strip()
    event = data.get("event", "").strip().lower()
    play = data.get("play", "").strip().upper()
    alert_time = data.get("time", "")

    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400

    inst = get_or_create_instrument(ticker)
    inst["lastUpdate"] = alert_time or datetime.now(timezone.utc).isoformat()

    if event == "entry":
        if not play or play not in PLAY_META:
            return jsonify({"error": f"Unknown play: {play}"}), 400

        # Move current active to passed if there was one
        if inst["active"] and inst["active"] != play:
            if inst["active"] not in inst["passed"]:
                inst["passed"].append(inst["active"])

        # Set new active play
        meta = PLAY_META[play]
        inst["active"] = play
        inst["activeDirection"] = meta["direction"]
        inst["activeDesc"] = meta["desc"]

        # Remove from candidates if it was there
        if play in inst["candidates"]:
            inst["candidates"].remove(play)

        app.logger.info(f"ENTRY: {ticker} {play} at {alert_time}")

    elif event == "candidate":
        if not play or play not in PLAY_META:
            return jsonify({"error": f"Unknown play: {play}"}), 400

        # Add to candidates if not already there and not active
        if play not in inst["candidates"] and play != inst["active"]:
            inst["candidates"].append(play)

        app.logger.info(f"CANDIDATE: {ticker} {play} at {alert_time}")

    elif event == "candidate_remove":
        if play in inst["candidates"]:
            inst["candidates"].remove(play)

        app.logger.info(f"CANDIDATE REMOVED: {ticker} {play} at {alert_time}")

    elif event == "exit":
        if inst["active"] == play:
            if play not in inst["passed"]:
                inst["passed"].append(play)
            inst["active"] = None
            inst["activeDirection"] = None
            inst["activeDesc"] = None

        app.logger.info(f"EXIT: {ticker} {play} at {alert_time}")

    elif event == "context":
        # Update range/width/dataSource context
        if "range" in data:
            inst["range"] = data["range"]
        if "width" in data:
            inst["width"] = data["width"]
        if "dataSource" in data:
            inst["dataSource"] = data["dataSource"]

        app.logger.info(f"CONTEXT: {ticker} range={inst['range']} width={inst['width']} data={inst['dataSource']}")

    elif event == "session_reset":
        reset_session(ticker)
        app.logger.info(f"SESSION RESET: {ticker}")

    else:
        return jsonify({"error": f"Unknown event: {event}"}), 400

    return jsonify({"status": "ok", "ticker": ticker, "event": event})


# ---- Dashboard data endpoint ----

@app.route("/api/state", methods=["GET"])
def get_state():
    """Return current state of all instruments for the dashboard."""
    # Return all instruments sorted by ticker
    result = sorted(instruments.values(), key=lambda x: x["ticker"])
    return jsonify({
        "instruments": result,
        "serverTime": datetime.now(timezone.utc).isoformat(),
    })


# ---- Instrument management ----

@app.route("/api/instruments", methods=["POST"])
def add_instrument():
    """Add a new instrument to track."""
    data = request.get_json(force=True)
    ticker = data.get("ticker", "").strip()
    exchange = data.get("exchange", "").strip()
    name = data.get("name", ticker).strip()

    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400

    if ticker not in instruments:
        instruments[ticker] = {
            "ticker": ticker,
            "exchange": exchange,
            "name": name,
            "active": None,
            "activeDirection": None,
            "activeDesc": None,
            "candidates": [],
            "passed": [],
            "range": None,
            "width": None,
            "dataSource": None,
            "lastUpdate": None,
        }

    return jsonify({"status": "ok", "ticker": ticker})


@app.route("/api/reset", methods=["POST"])
def reset_all():
    """Reset all instruments for a new session."""
    for ticker in instruments:
        reset_session(ticker)
    return jsonify({"status": "ok", "message": "All instruments reset"})


# ---- Serve dashboard ----

@app.route("/")
def serve_dashboard():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)


# ---- Health check ----

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "instruments": len(instruments)})


# ---- Initialize default instruments on startup ----

def init_defaults():
    for ticker in INSTRUMENT_DEFAULTS:
        get_or_create_instrument(ticker)


init_defaults()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
