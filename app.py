"""
Neural Nexus Command Center — Flask Backend
Run: python app.py  →  http://localhost:8080
"""

import os, math, time, random, threading, json
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_file, request
from flask_cors import CORS
from marl_router import MARLRouter

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
STATE_FILE     = os.path.join(BASE_DIR, "state.json")
app            = Flask(__name__)
CORS(app)

TOMTOM_API_KEY = "YLGrbeqrrvkTczVWNRbhtSDBARb2QWYw"
NUM_FLEETS     = 50
SIM_TICK_S     = 0.12      # simulation loop interval (seconds)
MAX_WORKERS    = 40         # parallel TomTom/OSRM fetch threads
TOMTOM_TIMEOUT = 3          # seconds per TomTom routing call
OSRM_TIMEOUT   = 4          # seconds per OSRM routing call
PERSIST_EVERY  = 10         # persist state every N sim ticks

router = MARLRouter(TOMTOM_API_KEY)

MUMBAI_ZONES = [
    {"name": "Andheri Tech Park",    "coords": [19.1136, 72.8697]},
    {"name": "Bandra Kurla Complex", "coords": [19.0596, 72.8295]},
    {"name": "Dadar Hub",            "coords": [19.0178, 72.8478]},
    {"name": "Powai Valley",         "coords": [19.1176, 72.9060]},
    {"name": "Fort Financial",       "coords": [18.9400, 72.8352]},
    {"name": "Borivali North",       "coords": [19.2307, 72.8567]},
    {"name": "Thane Outskirts",      "coords": [19.2183, 72.9780]},
    {"name": "Navi Mumbai Hub",      "coords": [19.0770, 72.9986]},
    {"name": "Colaba Port",          "coords": [18.9067, 72.8147]},
    {"name": "Chembur East",         "coords": [19.0522, 72.8996]},
]

# ── Shared simulation state ─────────────────────────────────────────────────
sim = {
    "running":   False,
    "deploying": False,
    "fleets":    [],
    "log":       [],
    "frame":     0,
    "stats":     {"active": 0, "delivered": 0, "god_mode": 0},
}
_lock = threading.Lock()
_flow_executor = ThreadPoolExecutor(max_workers=6)


# ── Haversine great-circle distance (km) ────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return round(R * 2 * math.asin(math.sqrt(min(1.0, a))), 1)


def _fetch_route(task):
    """
    Fetch one route in a thread.
    Chain: TomTom (live traffic) → OSRM (real roads, free) → skip fleet.
    """
    idx, start, dest = task

    # 1. Try TomTom with live traffic
    path, dist, eta = router.calculate_traffic_route(
        start, dest, timeout=TOMTOM_TIMEOUT
    )
    if path and len(path) >= 5:
        path = [[float(p[0]), float(p[1])] for p in path]
        return idx, path, round(dist, 1), int(eta)

    # 2. Fallback to OSRM — real road geometry, no API key needed
    path, dist, eta = router.calculate_osrm_route(
        start, dest, timeout=OSRM_TIMEOUT
    )
    if path and len(path) >= 5:
        path = [[float(p[0]), float(p[1])] for p in path]
        return idx, path, round(dist, 1), int(eta)

    # 3. Both failed — return no path
    return idx, None, 0, 0


# ── Generate a batch of candidate pairs ────────────────────────────────────
def _gen_candidates(n):
    cands = []
    attempts = 0
    while len(cands) < n and attempts < n * 20:
        attempts += 1
        sz = random.choice(MUMBAI_ZONES)
        dz = random.choice(MUMBAI_ZONES)
        if sz["name"] == dz["name"]:
            continue
        s = (sz["coords"][0] + random.uniform(-0.018, 0.018),
             sz["coords"][1] + random.uniform(-0.018, 0.018))
        d = (dz["coords"][0] + random.uniform(-0.018, 0.018),
             dz["coords"][1] + random.uniform(-0.018, 0.018))
        cands.append((sz, dz, s, d))
    return cands


# ── Swarm builder — guaranteed NUM_FLEETS ──────────────────────────────────
def build_swarm():
    fleets = []
    batch_num = 0

    # Keep batching until we have exactly NUM_FLEETS valid fleets
    while len(fleets) < NUM_FLEETS:
        batch_num += 1
        need       = NUM_FLEETS - len(fleets)
        # Over-generate by 40% to account for API failures
        cands      = _gen_candidates(int(need * 1.4))

        tasks = [(i, c[2], c[3]) for i, c in enumerate(cands)]
        results = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(_fetch_route, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                idx, path, dist, eta = fut.result()
                results[idx] = (path, dist, eta)

        for i, (sz, dz, s, d) in enumerate(cands):
            if len(fleets) >= NUM_FLEETS:
                break
            path, dist, eta = results.get(i, (None, 0, 5))
            if not path or len(path) < 5:
                continue

            prio  = random.choice([1, 2, 3])
            color = "medical" if prio == 1 else "normal"

            straight_km      = haversine(s[0], s[1], d[0], d[1])
            straight_eta     = max(1, round((straight_km / 30) * 60))
            time_saved_mins  = straight_eta - int(eta)
            road_overhead_pct = max(0, round(
                ((dist - straight_km) / max(straight_km, 0.1)) * 100
            ))

            fleets.append({
                "id":               f"AX-{len(fleets)+1:02d}",
                "prio":             prio,
                "fuel":             float(random.randint(40, 100)),
                "start":            list(s),
                "dest":             list(d),
                "zone":             dz["name"],
                "start_zone":       sz["name"],
                "route":            path,
                "current_step":     0,
                "progress":         0.0,
                "status":           "Active",
                "color":            color,
                "reroutes":         0,
                "swap_cooldown":    0,
                "eta":              eta,
                "dist_km":          dist,
                "straight_km":      straight_km,
                "straight_eta":     straight_eta,
                "time_saved_mins":  time_saved_mins,
                "road_overhead_pct": road_overhead_pct,
                "cur_lat":          path[0][0],
                "cur_lon":          path[0][1],
                "traffic_ratio":    1.0,
            })

    return fleets[:NUM_FLEETS]


# ── Simulation speeds ────────────────────────────────────────────────────────
_SPD = {"god": 0.40, "prio": 0.24, "norm": 0.13, "low": 0.05}


def _advance(f):
    route = f["route"]
    step  = f["current_step"]
    if step >= len(route) - 1:
        f["status"] = "Delivered"
        f["color"]  = "delivered"
        f["cur_lat"], f["cur_lon"] = f["dest"]
        return

    if "GOD MODE" in f["status"]:
        spd = _SPD["god"]
    elif f["fuel"] < 20:
        spd = _SPD["low"]
    elif f["prio"] == 1:
        spd = _SPD["prio"]
    else:
        spd = _SPD["norm"]

    if "GOD MODE" not in f["status"]:
        spd *= max(0.12, f.get("traffic_ratio", 1.0))

    f["progress"] += spd
    while f["progress"] >= 1.0 and f["current_step"] < len(route) - 1:
        f["current_step"] += 1
        f["progress"] -= 1.0
        if "GOD MODE" not in f["status"]:
            f["fuel"] = max(0.0, round(f["fuel"] - 0.18, 1))
        f["eta"] = max(0, f["eta"] - 1)

    s = f["current_step"]
    if s < len(route) - 1:
        lat1, lon1 = route[s]
        lat2, lon2 = route[s + 1]
        t = min(f["progress"], 1.0)
        f["cur_lat"] = lat1 + (lat2 - lat1) * t
        f["cur_lon"] = lon1 + (lon2 - lon1) * t
    else:
        f["cur_lat"], f["cur_lon"] = f["dest"]
        f["status"] = "Delivered"
        f["color"]  = "delivered"


def sim_tick():
    with _lock:
        if not sim["running"]:
            return
        fleets = sim["fleets"]
        sim["frame"] += 1

        # MARL policy (zero API calls — uses cached routes)
        for i in range(len(fleets)):
            for j in range(i + 1, len(fleets)):
                f1, f2 = fleets[i], fleets[j]
                if (f1["status"] == "Delivered" or f2["status"] == "Delivered" or
                        f1["swap_cooldown"] > 0 or f2["swap_cooldown"] > 0):
                    continue
                action, msg = router.evaluate_marl_policy(f1, f2)
                if action == "MERGE":
                    f1["route"] = f1["route"][f1["current_step"]:]
                    f1["current_step"] = 0; f1["progress"] = 0.0
                    f1["status"] = "Consolidated"; f2["status"] = "Reassigned"
                    f1["swap_cooldown"] = 100; f2["swap_cooldown"] = 100
                    f1["reroutes"] += 1; f2["reroutes"] += 1
                    if f1["prio"] == 1 or f2["prio"] == 1:
                        _log(msg)
                elif action == "FUEL_EXCHANGE":
                    r1 = f1["route"][f1["current_step"]:]
                    r2 = f2["route"][f2["current_step"]:]
                    f1["route"] = r2; f2["route"] = r1
                    f1["current_step"] = f2["current_step"] = 0
                    f1["progress"] = f2["progress"] = 0.0
                    f1["status"] = "Swapped"; f2["status"] = "Swapped"
                    f1["swap_cooldown"] = 50; f2["swap_cooldown"] = 50
                    f1["reroutes"] += 1; f2["reroutes"] += 1
                    if f1["prio"] == 1 or f2["prio"] == 1:
                        _log(msg)

        active = delivered = god_mode = 0
        for f in fleets:
            if f["swap_cooldown"] > 0:
                f["swap_cooldown"] -= 1

            if "Delivered" in f["status"]:
                delivered += 1
                continue

            # GOD MODE trigger for P1 fleets at risk
            if f["prio"] == 1 and "GOD MODE" not in f["status"]:
                if f["eta"] > 5 and f["fuel"] < 45:
                    f["status"] = "GOD MODE"
                    f["color"]  = "god"
                    _log(f"🚨 SLA BREACH — {f['id']} escalated to GOD MODE")

            if "GOD MODE" in f["status"]:
                f["fuel"] = max(20.0, f["fuel"])
                god_mode += 1
                f["color"] = "god"
            elif f["fuel"] < 20:
                if f["status"] in ("Active", "Low Fuel"):
                    f["status"] = "Low Fuel"
                    f["color"]  = "low_fuel"

            _advance(f)

            if "Delivered" in f["status"]:
                delivered += 1
            else:
                active += 1

        sim["stats"] = {"active": active, "delivered": delivered, "god_mode": god_mode}

        # Persist every PERSIST_EVERY ticks
        if sim["frame"] % PERSIST_EVERY == 0:
            _persist_state()


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    sim["log"].insert(0, f"[{ts}] {msg}")
    if len(sim["log"]) > 80:
        sim["log"] = sim["log"][:80]


def _persist_state():
    """Save fleet positions + simulation state to disk for refresh persistence."""
    try:
        snapshot = {
            "frame":   sim["frame"],
            "running": sim["running"],
            "stats":   sim["stats"],
            "fleets": [
                {
                    "id":               f["id"],
                    "prio":             f["prio"],
                    "fuel":             f["fuel"],
                    "start":            f["start"],
                    "dest":             f["dest"],
                    "zone":             f["zone"],
                    "start_zone":       f["start_zone"],
                    "route":            f["route"],
                    "current_step":     f["current_step"],
                    "progress":         f["progress"],
                    "status":           f["status"],
                    "color":            f["color"],
                    "reroutes":         f["reroutes"],
                    "swap_cooldown":    f["swap_cooldown"],
                    "eta":              f["eta"],
                    "dist_km":          f["dist_km"],
                    "straight_km":      f["straight_km"],
                    "straight_eta":     f["straight_eta"],
                    "time_saved_mins":  f["time_saved_mins"],
                    "road_overhead_pct": f["road_overhead_pct"],
                    "cur_lat":          f["cur_lat"],
                    "cur_lon":          f["cur_lon"],
                    "traffic_ratio":    f.get("traffic_ratio", 1.0),
                }
                for f in sim["fleets"]
            ],
        }
        with open(STATE_FILE, "w") as fp:
            json.dump(snapshot, fp)
    except Exception:
        pass  # Never crash the sim loop on IO failure


def _load_persisted_state():
    """On startup: resume from state.json if it exists and has valid fleet data."""
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE) as fp:
            data = json.load(fp)
        fleets = data.get("fleets", [])
        if len(fleets) < NUM_FLEETS:
            return False
        with _lock:
            sim["fleets"]  = fleets
            sim["frame"]   = data.get("frame", 0)
            sim["stats"]   = data.get("stats", {"active": 0, "delivered": 0, "god_mode": 0})
            sim["running"] = True
            _log("⚡ Session resumed — fleet positions restored")
        return True
    except Exception:
        return False


def _fetch_traffic_ratio(lat, lon):
    try:
        resp = requests.get(
            "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative/10/json",
            params={"key": TOMTOM_API_KEY, "point": f"{lat},{lon}", "unit": "KMPH"},
            timeout=4,
        )
        fd   = resp.json().get("flowSegmentData", {})
        cur  = float(fd.get("currentSpeed",  0))
        free = float(fd.get("freeFlowSpeed", 1))
        if free > 0:
            return round(min(1.0, max(0.0, cur / free)), 2)
    except Exception:
        pass
    return 1.0


def _traffic_poll_loop():
    """Background thread: polls TomTom Traffic Flow every 30 s for all active fleets."""
    while True:
        time.sleep(30)
        try:
            with _lock:
                snapshot = [
                    (f["id"], f["cur_lat"], f["cur_lon"], f["prio"])
                    for f in sim["fleets"]
                    if "Delivered" not in f["status"]
                ]
            if not snapshot:
                continue

            futs = {
                _flow_executor.submit(_fetch_traffic_ratio, lat, lon): (fid, prio)
                for fid, lat, lon, prio in snapshot
            }
            for fut in as_completed(futs, timeout=12):
                fid, prio = futs[fut]
                try:
                    ratio = fut.result()
                except Exception:
                    ratio = 1.0
                with _lock:
                    f = next((x for x in sim["fleets"] if x["id"] == fid), None)
                    if not f or "Delivered" in f["status"]:
                        continue
                    prev               = f.get("traffic_ratio", 1.0)
                    f["traffic_ratio"] = ratio

                    # Only escalate P1 fleets to GOD MODE on severe congestion
                    if ratio < 0.30 and prev >= 0.30 and prio == 1:
                        if "GOD MODE" not in f.get("status", ""):
                            f["status"] = "GOD MODE"
                            f["color"]  = "god"
                            _log(f"🚨 GRIDLOCK OVERRIDE — {fid} escalated to GOD MODE")
        except Exception:
            pass


threading.Thread(target=_traffic_poll_loop, daemon=True).start()


def _sim_loop():
    while True:
        try:
            sim_tick()
        except Exception:
            pass
        time.sleep(SIM_TICK_S)


threading.Thread(target=_sim_loop, daemon=True).start()


# ── Flask routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    with _lock:
        if sim["deploying"]:
            return jsonify({"ok": False, "msg": "Deploy already in progress"}), 400
        sim.update(running=False, deploying=True, fleets=[], log=[], frame=0,
                   stats={"active": 0, "delivered": 0, "god_mode": 0})

    def _bg():
        try:
            fleets = build_swarm()
            with _lock:
                sim["fleets"]    = fleets
                sim["running"]   = True
                sim["deploying"] = False
                _log(f"⚡ SYSTEM ONLINE — {len(fleets)} fleets deployed")
            _persist_state()
        except Exception as e:
            with _lock:
                sim["deploying"] = False
                _log(f"❌ Deploy error: {e}")

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Force clear persisted state and stop simulation (next deploy = fresh start)."""
    with _lock:
        sim.update(running=False, deploying=False, fleets=[], log=[], frame=0,
                   stats={"active": 0, "delivered": 0, "god_mode": 0})
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    with _lock:
        fleets = sim["fleets"]

        # ── Vehicle GeoJSON ─────────────────────────────────────────────────
        veh_feats = []
        for f in fleets:
            total = max(len(f["route"]) - 1, 1)
            pct   = round(f["current_step"] / total * 100)
            veh_feats.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [f["cur_lon"], f["cur_lat"]],
                },
                "properties": {
                    "id":     f["id"],
                    "status": f["status"],
                    "color":  f.get("color", "normal"),
                    "fuel":   f["fuel"],
                    "prio":   f["prio"],
                    "eta":    f["eta"],
                    "zone":   f["zone"],
                    "pct":    pct,
                },
            })

        # ── Route GeoJSON (remaining paths, subsampled to max 80 points) ───
        rte_feats = []
        for f in fleets:
            if "Delivered" in f["status"]:
                continue
            step   = f["current_step"]
            remain = f["route"][step:]
            # Subsample: at most 80 waypoints per route
            stride = max(1, len(remain) // 80)
            coords = [[p[1], p[0]] for p in remain[::stride]]
            if len(coords) < 2:
                continue
            coords[0] = [f["cur_lon"], f["cur_lat"]]  # snap to interpolated pos
            rte_feats.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "id":    f["id"],
                    "color": f.get("color", "normal"),
                },
            })

        # ── Lightweight fleet list for sidebar ──────────────────────────────
        fleet_list = [{
            "id":     f["id"],
            "status": f["status"],
            "color":  f.get("color", "normal"),
            "prio":   f["prio"],
            "fuel":   f["fuel"],
            "eta":    f["eta"],
            "zone":   f["zone"],
            "pct":    round(f["current_step"] / max(len(f["route"]) - 1, 1) * 100),
        } for f in fleets]

        return jsonify({
            "running":   sim["running"],
            "deploying": sim["deploying"],
            "frame":     sim["frame"],
            "stats":     sim["stats"],
            "log":       sim["log"][:30],
            "vehicles":  {"type": "FeatureCollection", "features": veh_feats},
            "routes":    {"type": "FeatureCollection", "features": rte_feats},
            "fleets":    fleet_list,
        })


@app.route("/api/fleet/<fid>")
def api_fleet(fid):
    with _lock:
        f = next((x for x in sim["fleets"] if x["id"] == fid), None)
        if not f:
            return jsonify({"error": "not found"}), 404
        step, total = f["current_step"], len(f["route"])
        stride_t = max(1, step // 60)
        stride_r = max(1, (total - step) // 60)
        return jsonify({
            "id":              f["id"],
            "status":          f["status"],
            "color":           f.get("color", "normal"),
            "prio":            f["prio"],
            "fuel":            f["fuel"],
            "eta":             f["eta"],
            "zone":            f["zone"],
            "start_zone":      f.get("start_zone", ""),
            "dist_km":         f.get("dist_km", 0),
            "reroutes":        f["reroutes"],
            "cur_lat":         f["cur_lat"],
            "cur_lon":         f["cur_lon"],
            "dest":            f["dest"],
            "start":           f.get("start", []),
            "pct":             round(step / max(total - 1, 1) * 100),
            "current_step":    step,
            "total_steps":     total,
            "route_traveled":  [[p[1], p[0]] for p in f["route"][:step + 1:stride_t]],
            "route_remaining": [[p[1], p[0]] for p in f["route"][step::stride_r]],
            "straight_km":     f.get("straight_km", 0),
            "straight_eta":    f.get("straight_eta", 0),
            "time_saved_mins": f.get("time_saved_mins", 0),
            "road_overhead_pct": f.get("road_overhead_pct", 0),
            "traffic_ratio":   f.get("traffic_ratio", 1.0),
        })


if __name__ == "__main__":
    print("\n" + "═" * 50)
    print("  🌐  Neural Nexus Command Center")
    print("  →   http://localhost:8080")
    print("═" * 50 + "\n")

    # Always start with an empty map — browser refresh (F5) preserves state naturally
    # because the server process keeps running and state lives in RAM.
    # Only a Deploy click populates the map.
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
    print("  ℹ️   Ready — click Deploy to initialize the fleet swarm\n")

    app.run(debug=False, port=8080, threaded=True)

