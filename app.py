import json
import os
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

OBA_BASE = "https://api.pugetsound.onebusaway.org/api/where"
OBA_KEY = os.environ.get("OBA_API_KEY", "TEST")
CONFIG_PATH = Path(__file__).parent / "config.json"
KCM_AGENCY_ID = "1"


# --- Persistence ---

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"stops": []}


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


# --- OBA API helpers ---

def oba_get(endpoint, params=None):
    """Call OneBusAway API. Returns parsed JSON or None on error."""
    url = f"{OBA_BASE}/{endpoint}.json"
    p = {"key": OBA_KEY}
    if params:
        p.update(params)
    try:
        r = requests.get(url, params=p, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == 200:
            return data.get("data")
        return None
    except Exception:
        return None


def search_routes(query):
    """Search KCM routes matching a query string (route number or name)."""
    data = oba_get(f"routes-for-agency/{KCM_AGENCY_ID}")
    if not data:
        return []
    routes = data.get("list", [])
    q = query.lower().strip()
    matches = []
    for r in routes:
        short = (r.get("shortName") or "").lower()
        long_name = (r.get("longName") or "").lower()
        desc = (r.get("description") or "").lower()
        if q in short or q in long_name or q in desc:
            matches.append({
                "id": r["id"],
                "shortName": r.get("shortName", ""),
                "longName": r.get("longName", ""),
                "description": r.get("description", ""),
            })
    return matches


def get_stops_for_route(route_id):
    """Get stops for a route, grouped by direction."""
    data = oba_get(f"stops-for-route/{route_id}")
    if not data:
        return []

    # Build stop lookup from references
    refs = data.get("references", {})
    stop_map = {}
    for s in refs.get("stops", []):
        stop_map[s["id"]] = {
            "id": s["id"],
            "name": s.get("name", ""),
            "direction": s.get("direction", ""),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
        }

    # Use stop groupings for direction info
    groups = []
    for sg in data.get("entry", {}).get("stopGroupings", []):
        for group in sg.get("stopGroups", []):
            direction_name = group.get("name", {}).get("name", "")
            stop_ids = group.get("stopIds", [])
            stops = [stop_map[sid] for sid in stop_ids if sid in stop_map]
            groups.append({
                "direction": direction_name,
                "stops": stops,
            })
    return groups


def get_arrivals_for_stop(stop_id):
    """Get real-time arrivals for a stop."""
    data = oba_get(f"arrivals-and-departures-for-stop/{stop_id}",
                   {"minutesBefore": 5, "minutesAfter": 60})
    if not data:
        return []

    refs = data.get("references", {})
    route_map = {}
    for r in refs.get("routes", []):
        route_map[r["id"]] = {
            "shortName": r.get("shortName", ""),
            "longName": r.get("longName", ""),
        }

    arrivals = []
    for ad in data.get("entry", {}).get("arrivalsAndDepartures", []):
        route_info = route_map.get(ad.get("routeId"), {})
        arrivals.append({
            "routeId": ad.get("routeId", ""),
            "routeShortName": ad.get("routeShortName") or route_info.get("shortName", ""),
            "routeLongName": route_info.get("longName", ""),
            "tripHeadsign": ad.get("tripHeadsign", ""),
            "stopId": ad.get("stopId", ""),
            "predicted": ad.get("predicted", False),
            "scheduledArrivalTime": ad.get("scheduledArrivalTime", 0),
            "scheduledDepartureTime": ad.get("scheduledDepartureTime", 0),
            "predictedArrivalTime": ad.get("predictedArrivalTime", 0),
            "predictedDepartureTime": ad.get("predictedDepartureTime", 0),
            "distanceFromStop": ad.get("distanceFromStop"),
            "numberOfStopsAway": ad.get("numberOfStopsAway"),
        })
    return arrivals


def get_effective_time(arrival):
    """Get the actual expected departure time - predicted if available, else scheduled.

    This is the KEY function that solves the delayed-bus problem:
    A bus scheduled at 7:00 but delayed to 7:30 should show up at 7:25.
    We use predicted time when available, falling back to scheduled.
    """
    if arrival["predicted"] and arrival["predictedDepartureTime"] > 0:
        return arrival["predictedDepartureTime"]
    return arrival["scheduledDepartureTime"]


# --- Flask routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/departures")
def departures():
    direction = request.args.get("direction", "in")
    config = load_config()
    now_ms = int(time.time() * 1000)

    all_departures = []
    for saved_stop in config["stops"]:
        if saved_stop["direction"] != direction:
            continue

        arrivals = get_arrivals_for_stop(saved_stop["id"])
        route_filter = saved_stop.get("route_filter")

        for a in arrivals:
            # Apply route filter if set
            if route_filter and a["routeId"] not in route_filter:
                continue

            eff_time = get_effective_time(a)

            # CRITICAL: Show if effective time is in the future
            # This catches delayed buses that other apps miss
            if eff_time <= now_ms:
                continue

            delay_ms = 0
            if a["predicted"] and a["predictedDepartureTime"] > 0:
                delay_ms = a["predictedDepartureTime"] - a["scheduledDepartureTime"]

            all_departures.append({
                "routeShortName": a["routeShortName"],
                "routeLongName": a["routeLongName"],
                "tripHeadsign": a["tripHeadsign"],
                "stopId": a["stopId"],
                "stopName": saved_stop["name"],
                "effectiveTime": eff_time,
                "scheduledTime": a["scheduledDepartureTime"],
                "predicted": a["predicted"],
                "delayMinutes": round(delay_ms / 60000) if delay_ms > 0 else 0,
                "minutesAway": round((eff_time - now_ms) / 60000),
                "distanceFromStop": a.get("distanceFromStop"),
                "numberOfStopsAway": a.get("numberOfStopsAway"),
            })

    # Sort by effective time - soonest first
    all_departures.sort(key=lambda d: d["effectiveTime"])

    # Mark best option
    for i, dep in enumerate(all_departures):
        dep["recommended"] = (i == 0)

    return jsonify({"departures": all_departures, "timestamp": now_ms})


@app.route("/api/routes/search")
def route_search():
    q = request.args.get("q", "")
    if len(q) < 1:
        return jsonify({"routes": []})
    return jsonify({"routes": search_routes(q)})


@app.route("/api/stops/for-route")
def stops_for_route():
    route_id = request.args.get("route_id", "")
    if not route_id:
        return jsonify({"groups": []})
    return jsonify({"groups": get_stops_for_route(route_id)})


@app.route("/api/stops/saved")
def saved_stops():
    config = load_config()
    return jsonify({"stops": config["stops"]})


@app.route("/api/stops", methods=["POST"])
def add_stop():
    body = request.get_json()
    if not body or not body.get("id") or not body.get("direction"):
        return jsonify({"error": "id and direction required"}), 400

    config = load_config()

    # Don't add duplicates (same stop + direction)
    for s in config["stops"]:
        if s["id"] == body["id"] and s["direction"] == body["direction"]:
            return jsonify({"error": "Stop already saved for this direction"}), 409

    stop = {
        "id": body["id"],
        "name": body.get("name", ""),
        "direction": body["direction"],
    }
    if body.get("route_filter"):
        stop["route_filter"] = body["route_filter"]

    config["stops"].append(stop)
    save_config(config)
    return jsonify({"ok": True, "stop": stop}), 201


@app.route("/api/stops/<path:stop_id>", methods=["DELETE"])
def delete_stop(stop_id):
    direction = request.args.get("direction", "")
    config = load_config()
    config["stops"] = [
        s for s in config["stops"]
        if not (s["id"] == stop_id and (not direction or s["direction"] == direction))
    ]
    save_config(config)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
