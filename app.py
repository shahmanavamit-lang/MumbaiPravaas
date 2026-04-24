"""
MumbaiPravaas Command Center — Flask Backend
Run: python app.py  →  http://localhost:8080
"""
import os, math, time, random, threading, json, requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_file, request
from flask_cors import CORS
from marl_router import MARLRouter

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
app        = Flask(__name__)
CORS(app)

TOMTOM_API_KEY  = "YLGrbeqrrvkTczVWNRbhtSDBARb2QWYw"
NUM_FLEETS      = 50
NUM_SLOTS       = 5
FLEETS_PER_SLOT = 10
SIM_TICK_S      = 0.10
PERSIST_EVERY   = 10
OSRM_TIMEOUT    = 3
TOMTOM_TIMEOUT  = 2

router = MARLRouter(TOMTOM_API_KEY)

# Pre-fetched route cache — populated at startup so deploy is instant
_route_cache   = []   # list of (path, dist, eta, sz, dz) tuples
_cache_lock    = threading.Lock()
_cache_ready   = threading.Event()

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
    {"name": "Malad West",           "coords": [19.1870, 72.8490]},
    {"name": "Kurla Junction",       "coords": [19.0726, 72.8791]},
    {"name": "Ghatkopar East",       "coords": [19.0864, 72.9081]},
    {"name": "Vikhroli",             "coords": [19.1041, 72.9262]},
    {"name": "Goregaon East",        "coords": [19.1630, 72.8587]},
    {"name": "Juhu Beach",           "coords": [19.1075, 72.8263]},
    {"name": "Marine Drive",         "coords": [18.9433, 72.8231]},
    {"name": "Gateway of India",     "coords": [18.9217, 72.8347]},
    {"name": "Chhatrapati Shivaji Terminus", "coords": [18.9398, 72.8354]},
    {"name": "Elephanta Caves",      "coords": [18.9633, 72.9315]},
    {"name": "Siddhivinayak Temple", "coords": [19.0330, 72.8296]},
    {"name": "Haji Ali Dargah",      "coords": [18.9777, 72.8091]},
    {"name": "Chor Bazaar",          "coords": [18.9600, 72.8270]},
    {"name": "Crawford Market",      "coords": [18.9471, 72.8347]},
    {"name": "Mahalaxmi Racecourse", "coords": [18.9790, 72.8170]},
]

sim = {
    "running": False, "deploying": False,
    "fleets": [], "log": [], "frame": 0,
    "stats": {"active": 0, "delivered": 0, "god_mode": 0},
    "slots_deployed": 0,
}
_lock          = threading.Lock()
_flow_executor = ThreadPoolExecutor(max_workers=6)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return round(R * 2 * math.asin(math.sqrt(min(1.0, a))), 1)


def _fetch_osrm(start, dest):
    """Fetch road route from OSRM public API."""
    try:
        url = (f"http://router.project-osrm.org/route/v1/driving/"
               f"{start[1]},{start[0]};{dest[1]},{dest[0]}"
               f"?overview=full&geometries=geojson&steps=false")
        r = requests.get(url, timeout=OSRM_TIMEOUT)
        data = r.json()
        if data.get("code") == "Ok":
            coords = data["routes"][0]["geometry"]["coordinates"]
            path = [[c[1], c[0]] for c in coords]
            dist = data["routes"][0]["distance"] / 1000
            dur  = data["routes"][0]["duration"] / 60
            if len(path) >= 5:
                return path, round(dist, 1), int(dur)
    except Exception:
        pass
    return None, 0, 0


def _fetch_route_task(task):
    idx, start, dest = task
    # Try TomTom first
    path, dist, eta = router.calculate_traffic_route(start, dest, timeout=TOMTOM_TIMEOUT)
    if path and len(path) >= 5:
        return idx, [[float(p[0]), float(p[1])] for p in path], round(dist,1), int(eta)
    # Fallback: OSRM
    path, dist, eta = _fetch_osrm(start, dest)
    if path and len(path) >= 5:
        return idx, path, dist, eta
    return idx, None, 0, 0


def _gen_candidates(n):
    """Generate n origin-destination pairs with guaranteed destination diversity.

    Uses a shuffled rotating destination pool so every zone is visited roughly
    equally — prevents any single zone (e.g. Colaba) from dominating.
    """
    cands   = []
    attempts = 0

    # Build a randomised destination pool that covers all zones evenly
    pool_size   = max(n * 2, len(MUMBAI_ZONES) * 4)
    dest_pool   = MUMBAI_ZONES * (pool_size // len(MUMBAI_ZONES) + 1)
    dest_pool   = dest_pool[:pool_size]
    random.shuffle(dest_pool)
    pool_idx    = 0

    while len(cands) < n and attempts < n * 30:
        attempts += 1

        sz = random.choice(MUMBAI_ZONES)

        # Pick destination from the rotating pool; skip same zone as origin
        for _ in range(len(MUMBAI_ZONES)):
            dz = dest_pool[pool_idx % len(dest_pool)]
            pool_idx += 1
            if dz["name"] != sz["name"]:
                break
        else:
            # Fallback: any zone that isn't the origin
            dz = random.choice([z for z in MUMBAI_ZONES if z["name"] != sz["name"]])

        # Small jitter (±0.004°≈400 m) keeps points on-road;
        # large offsets (old ±0.012°) often snap to sea/airport.
        s = (sz["coords"][0] + random.uniform(-0.004, 0.004),
             sz["coords"][1] + random.uniform(-0.004, 0.004))
        d = (dz["coords"][0] + random.uniform(-0.004, 0.004),
             dz["coords"][1] + random.uniform(-0.004, 0.004))
        cands.append((sz, dz, s, d))

    return cands


def _fetch_route_with_centroid_fallback(sz, dz, s, d):
    """Try jittered coords first; if that fails, retry with exact zone centroids.
    This prevents OSRM from snapping to ocean/airport when jittered points are off-road.
    Returns (path, dist, eta) or (None, 0, 0).
    """
    # Attempt 1: jittered coordinates
    path, dist, eta = _fetch_osrm(s, d)
    if path and len(path) >= 5:
        return path, dist, eta

    # Attempt 2: TomTom with jittered coords
    path, dist, eta = router.calculate_traffic_route(s, d, timeout=TOMTOM_TIMEOUT)
    if path and len(path) >= 5:
        return [[float(p[0]), float(p[1])] for p in path], round(dist, 1), int(eta)

    # Attempt 3: OSRM with exact zone centroids (guaranteed on-road)
    sc = (sz["coords"][0], sz["coords"][1])
    dc = (dz["coords"][0], dz["coords"][1])
    path, dist, eta = _fetch_osrm(sc, dc)
    if path and len(path) >= 5:
        return path, dist, eta

    return None, 0, 0


def _make_fleet_record(fleet_num, slot_idx, path, dist, eta, sz, dz):
    """Build a single fleet dict from a resolved route."""
    sc = (sz["coords"][0], sz["coords"][1])
    dc = (dz["coords"][0], dz["coords"][1])
    str_km  = haversine(sc[0], sc[1], dc[0], dc[1])
    str_eta = max(1, round((str_km / 25) * 60))
    prio    = random.choice([1, 2, 3])
    return {
        "id":               f"AX-{fleet_num:02d}",
        "slot":             slot_idx + 1,
        "prio":             prio,
        "fuel":             float(random.randint(60, 100)),
        "start":            list(sc),
        "dest":             list(dc),
        "zone":             dz["name"],
        "start_zone":       sz["name"],
        "route":            path,
        "current_step":     0,
        "progress":         0.0,
        "status":           "Active",
        "color":            "medical" if prio == 1 else "normal",
        "reroutes":         0,
        "swap_cooldown":    0,
        "eta":              eta,
        "dist_km":          dist,
        "straight_km":      str_km,
        "straight_eta":     str_eta,
        "time_saved_mins":  max(0, str_eta - int(eta)),
        "road_overhead_pct": max(0, round(((dist - str_km) / max(str_km, 0.1)) * 100)),
        "cur_lat":          path[0][0],
        "cur_lon":          path[0][1],
        "traffic_ratio":    1.0,
        "custom":           False,
        "launch_delay":     0,   # seconds to wait before this fleet starts moving
    }


def _prefetch_all_routes():
    """Pre-fetch NUM_FLEETS road routes in parallel at startup.
    Stores results in _route_cache so deploy() is instant (<1s).
    """
    print("[MumbaiPravaas] 🔄 Pre-fetching road routes in background…")
    # We need 60 routes (50 + 20% spares)
    need  = NUM_FLEETS + 20
    cands = _gen_candidates(need)

    def _task(args):
        i, sz, dz, s, d = args
        path, dist, eta = _fetch_route_with_centroid_fallback(sz, dz, s, d)
        return i, sz, dz, path, dist, eta

    results = []
    _first_batch_signalled = False
    with ThreadPoolExecutor(max_workers=30) as ex:
        futs = {ex.submit(_task, (i, sz, dz, s, d)): i
                for i, (sz, dz, s, d) in enumerate(cands)}
        for fut in as_completed(futs, timeout=45):
            try:
                i, sz, dz, path, dist, eta = fut.result()
                if path and len(path) >= 5:
                    results.append((path, dist, eta, sz, dz))
                    with _cache_lock:
                        _route_cache.append((path, dist, eta, sz, dz))
                    # Signal as soon as we have enough for the first slot (~10 routes)
                    if not _first_batch_signalled and len(results) >= FLEETS_PER_SLOT:
                        _cache_ready.set()
                        _first_batch_signalled = True
                        print(f"[MumbaiPravaas] ⚡ First {FLEETS_PER_SLOT} routes ready — deploy unlocked!")
                    if len(results) >= NUM_FLEETS + 10:
                        break
            except Exception:
                pass

    if not _first_batch_signalled:
        _cache_ready.set()   # ensure event is always set even on failure
    print(f"[MumbaiPravaas] ✅ Route cache complete — {len(results)} routes pre-fetched")

# Thread is started AFTER _log is defined — see below build_slot()


def _select_slot_route_entries(slot_idx):
    """Choose a diverse set of cached routes for a deploy slot.

    This prevents every slot from reusing the exact same traffic corridor
    and encourages distinct origin/destination coverage across Mumbai.
    """
    with _cache_lock:
        candidates = list(_route_cache)

    if not candidates:
        return []

    random.shuffle(candidates)
    chosen = []
    used_starts = set()
    used_dests = set()

    # First pass: choose route entries with distinct start/dest zones.
    for path, dist, eta, sz, dz in candidates:
        if len(chosen) >= FLEETS_PER_SLOT:
            break
        if sz["name"] in used_starts or dz["name"] in used_dests:
            continue
        chosen.append((path, dist, eta, sz, dz))
        used_starts.add(sz["name"])
        used_dests.add(dz["name"])

    # If we still need fleets, add remaining entries without strict distinctness.
    for entry in candidates:
        if len(chosen) >= FLEETS_PER_SLOT:
            break
        if entry not in chosen:
            chosen.append(entry)

    return chosen


def build_slot(slot_idx):
    """Build FLEETS_PER_SLOT fleets instantly from the pre-fetched route cache.
    Falls back to live OSRM only if the cache is not yet ready or short.
    Target: complete in under 3 seconds.
    """
    fleets  = []
    base_id = slot_idx * FLEETS_PER_SLOT

    # Wait at most 4 s for the cache, then fall back to whatever is ready.
    _cache_ready.wait(timeout=4)

    entries = _select_slot_route_entries(slot_idx)
    if not entries:
        with _cache_lock:
            entries = list(_route_cache)
        random.shuffle(entries)

    # Build fleet records — wave stagger: each fleet delayed 0.25s within slot
    used = 0
    for path, dist, eta, sz, dz in entries:
        if used >= FLEETS_PER_SLOT:
            break
        f = _make_fleet_record(base_id + used + 1, slot_idx, path, dist, eta, sz, dz)
        f["launch_delay"] = used * 0.25   # 0s, 0.25s, 0.5s … 2.25s stagger
        fleets.append(f)
        used += 1

    # Safety pad: if cache was empty, do a quick live fetch
    extra = 0
    while len(fleets) < FLEETS_PER_SLOT and extra < 10:
        extra += 1
        sz = random.choice(MUMBAI_ZONES)
        dz = random.choice([z for z in MUMBAI_ZONES if z["name"] != sz["name"]])
        sc = tuple(sz["coords"]); dc = tuple(dz["coords"])
        path, dist, eta = _fetch_osrm(sc, dc)
        if not path or len(path) < 5:
            continue
        f = _make_fleet_record(base_id + len(fleets) + 1, slot_idx, path, dist, eta, sz, dz)
        f["launch_delay"] = len(fleets) * 0.25
        fleets.append(f)

    return fleets


# Speeds: steps-per-tick (tick=0.10s). Boosted so movement is clearly visible.
# Each step is ~20-50m on an OSRM polyline; norm≈0.18 → ~36-90m/tick → ~36-90 km/s sim-time
# which gives a nice visible glide across the map at normal zoom.
_SPD = {"god": 0.55, "prio": 0.30, "norm": 0.18, "low": 0.07}


def _advance(f):
    route = f["route"]
    step  = f["current_step"]

    # Wave stagger: hold the fleet at its start until launch_delay elapses
    if f.get("launch_delay", 0) > 0:
        f["launch_delay"] = max(0, f["launch_delay"] - SIM_TICK_S)
        return

    if step >= len(route) - 1:
        f["status"] = "Delivered"; f["color"] = "delivered"
        f["cur_lat"], f["cur_lon"] = f["dest"]
        return
    if f["fuel"] <= 0 and "GOD MODE" not in f["status"]:
        f["status"] = "Out of Fuel"; f["color"] = "out_of_fuel"; return

    spd = (_SPD["god"] if "GOD MODE" in f["status"]
           else _SPD["low"] if f["fuel"] < 20
           else _SPD["prio"] if f["prio"] == 1
           else _SPD["norm"])
    # Traffic ratio dampens speed but floors at 30% to keep movement visible
    if "GOD MODE" not in f["status"]:
        spd *= max(0.30, f.get("traffic_ratio", 1.0))

    f["progress"] += spd
    steps_moved = 0
    while f["progress"] >= 1.0 and f["current_step"] < len(route) - 1:
        f["current_step"] += 1
        f["progress"] -= 1.0
        steps_moved += 1
        if "GOD MODE" not in f["status"]:
            f["fuel"] = max(0.0, round(f["fuel"] - 0.04, 2))
    if steps_moved > 0:
        f["eta"] = max(0, f["eta"] - 1)

    s = f["current_step"]
    if s < len(route) - 1:
        lat1, lon1 = route[s]; lat2, lon2 = route[s+1]
        t = min(f["progress"], 1.0)
        f["cur_lat"] = lat1 + (lat2-lat1)*t
        f["cur_lon"] = lon1 + (lon2-lon1)*t
    else:
        f["cur_lat"], f["cur_lon"] = f["dest"]
        f["status"] = "Delivered"; f["color"] = "delivered"


def sim_tick():
    with _lock:
        if not sim["running"]: return
        fleets = sim["fleets"]
        sim["frame"] += 1

        # MARL policy: only MERGE is safe (both fleets keep their own road paths).
        # FUEL_EXCHANGE (route swap) is intentionally disabled — swapping route arrays
        # between two arbitrary fleets sends vehicles off-road through oceans/buildings.
        for i in range(len(fleets)):
            for j in range(i+1, len(fleets)):
                f1, f2 = fleets[i], fleets[j]
                if (f1["status"] == "Delivered" or f2["status"] == "Delivered"
                        or f1["swap_cooldown"] > 0 or f2["swap_cooldown"] > 0):
                    continue
                action, msg = router.evaluate_marl_policy(f1, f2)
                if action == "MERGE":
                    # Trim the leading already-traveled segment so the vehicle
                    # continues smoothly from its current position.
                    f1["route"] = f1["route"][f1["current_step"]:]
                    f1["current_step"] = 0; f1["progress"] = 0.0
                    f1["status"] = "Consolidated"; f2["status"] = "Reassigned"
                    f1["swap_cooldown"] = 100; f2["swap_cooldown"] = 100
                    f1["reroutes"] += 1; f2["reroutes"] += 1
                # FUEL_EXCHANGE disabled — route swap causes off-road movement

        active = delivered = god_mode = 0
        for f in fleets:
            if f["swap_cooldown"] > 0: f["swap_cooldown"] -= 1
            if "Delivered" in f["status"]:
                delivered += 1; continue
            if f["prio"] == 1 and "GOD MODE" not in f["status"]:
                if f["eta"] > 5 and f["fuel"] < 45:
                    f["status"] = "GOD MODE"; f["color"] = "god"
                    _log(f"🚨 SLA BREACH — {f['id']} escalated to GOD MODE")
            if "GOD MODE" in f["status"]:
                f["fuel"] = max(20.0, f["fuel"]); god_mode += 1; f["color"] = "god"
            elif f["fuel"] < 20 and f["status"] in ("Active","Low Fuel"):
                f["status"] = "Low Fuel"; f["color"] = "low_fuel"
            _advance(f)
            if "Delivered" in f["status"]: delivered += 1
            else: active += 1

        sim["stats"] = {"active": active, "delivered": delivered, "god_mode": god_mode}
        if sim["frame"] % PERSIST_EVERY == 0:
            _persist_state()


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    sim["log"].insert(0, f"[{ts}] {msg}")
    if len(sim["log"]) > 80: sim["log"] = sim["log"][:80]


# Start route pre-fetch NOW that _log is defined
threading.Thread(target=_prefetch_all_routes, daemon=True).start()


def _fleet_snapshot(f):
    return {k: f[k] for k in [
        "id","slot","prio","fuel","start","dest","zone","start_zone",
        "route","current_step","progress","status","color","reroutes",
        "swap_cooldown","eta","dist_km","straight_km","straight_eta",
        "time_saved_mins","road_overhead_pct","cur_lat","cur_lon","traffic_ratio","custom"
    ] if k in f}


def _persist_state():
    try:
        snap = {
            "frame": sim["frame"], "running": sim["running"],
            "stats": sim["stats"], "slots_deployed": sim["slots_deployed"],
            "fleets": [_fleet_snapshot(f) for f in sim["fleets"]],
        }
        with open(STATE_FILE, "w") as fp: json.dump(snap, fp)
    except Exception: pass


def _load_persisted_state():
    if not os.path.exists(STATE_FILE): return False
    try:
        with open(STATE_FILE) as fp: data = json.load(fp)
        fleets = data.get("fleets", [])
        if not fleets: return False
        with _lock:
            sim["fleets"]         = fleets
            sim["frame"]          = data.get("frame", 0)
            sim["stats"]          = data.get("stats", {"active":0,"delivered":0,"god_mode":0})
            sim["slots_deployed"] = data.get("slots_deployed", 0)
            sim["running"]        = True
            _log(f"⚡ Session resumed — {len(fleets)} fleets restored")
        return True
    except Exception: return False


def _fetch_traffic_ratio(lat, lon):
    try:
        r = requests.get(
            "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative/10/json",
            params={"key": TOMTOM_API_KEY, "point": f"{lat},{lon}", "unit": "KMPH"}, timeout=4)
        fd = r.json().get("flowSegmentData", {})
        cur = float(fd.get("currentSpeed", 0)); free = float(fd.get("freeFlowSpeed", 1))
        if free > 0: return round(min(1.0, max(0.0, cur/free)), 2)
    except Exception: pass
    return 1.0


def _traffic_poll_loop():
    while True:
        time.sleep(30)
        try:
            with _lock:
                snap = [(f["id"],f["cur_lat"],f["cur_lon"],f["prio"])
                        for f in sim["fleets"] if "Delivered" not in f["status"]]
            if not snap: continue
            futs = {_flow_executor.submit(_fetch_traffic_ratio, lat, lon): (fid, prio)
                    for fid, lat, lon, prio in snap}
            for fut in as_completed(futs, timeout=12):
                fid, prio = futs[fut]
                ratio = 1.0
                try: ratio = fut.result()
                except Exception: pass
                with _lock:
                    f = next((x for x in sim["fleets"] if x["id"]==fid), None)
                    if not f or "Delivered" in f["status"]: continue
                    prev = f.get("traffic_ratio", 1.0); f["traffic_ratio"] = ratio
                    if ratio < 0.30 and prev >= 0.30 and prio == 1:
                        if "GOD MODE" not in f.get("status",""):
                            f["status"]="GOD MODE"; f["color"]="god"
                            _log(f"🚨 GRIDLOCK OVERRIDE — {fid} escalated to GOD MODE")
        except Exception: pass


threading.Thread(target=_traffic_poll_loop, daemon=True).start()


def _sim_loop():
    while True:
        try: sim_tick()
        except Exception: pass
        time.sleep(SIM_TICK_S)


threading.Thread(target=_sim_loop, daemon=True).start()


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index(): return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    with _lock:
        if sim["deploying"]:
            return jsonify({"ok": False, "msg": "Deploy in progress"}), 400
        if sim["slots_deployed"] >= NUM_SLOTS:
            return jsonify({"ok": False, "msg": "All slots deployed", "slots_deployed": 5}), 400
        slot_idx = sim["slots_deployed"]
        sim["deploying"] = True

    def _bg():
        try:
            new_fleets = build_slot(slot_idx)
            with _lock:
                sim["fleets"].extend(new_fleets)
                sim["slots_deployed"] += 1
                sim["running"]   = True
                sim["deploying"] = False
                total = sim["slots_deployed"] * FLEETS_PER_SLOT
                _log(f"⚡ SLOT {sim['slots_deployed']}/5 — {len(new_fleets)} fleets launched ({total}/50)")
            _persist_state()
        except Exception as e:
            with _lock:
                sim["deploying"] = False
                _log(f"❌ Deploy error: {e}")
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "slot": slot_idx+1})


@app.route("/api/custom-route", methods=["POST"])
def api_custom_route():
    """Dispatch a single custom fleet between user-specified locations."""
    body   = request.get_json(force=True)
    origin = (body.get("origin") or "").strip()
    dest   = (body.get("destination") or "").strip()
    if not origin or not dest:
        return jsonify({"ok": False, "msg": "Origin and destination required"}), 400

    def geocode(place):
        """3-tier geocoder: TomTom → Nominatim (OSM) → MUMBAI_ZONES fuzzy match."""
        # ── Tier 1: TomTom Search ──────────────────────────────────────────────
        try:
            r = requests.get(
                f"https://api.tomtom.com/search/2/search/{requests.utils.quote(place)}.json",
                params={"key": TOMTOM_API_KEY, "countrySet": "IN",
                        "limit": 1, "lat": 19.07, "lon": 72.87, "radius": 80000},
                timeout=5)
            res = r.json().get("results", [])
            if res:
                pos = res[0]["position"]
                return [pos["lat"], pos["lon"]]
        except Exception:
            pass

        # ── Tier 2: Nominatim (OpenStreetMap) — free, no key needed ───────────
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place + ", Mumbai, India", "format": "json",
                        "limit": 1, "countrycodes": "in",
                        "viewbox": "72.55,18.80,73.50,19.50", "bounded": 1},
                headers={"User-Agent": "MumbaiPravaas/1.0"},
                timeout=6)
            results = r.json()
            if results:
                return [float(results[0]["lat"]), float(results[0]["lon"])]
        except Exception:
            pass

        # ── Tier 3: Fuzzy match against known MUMBAI_ZONES ────────────────────
        query = place.lower()
        best, best_score = None, 0
        for z in MUMBAI_ZONES:
            name_lower = z["name"].lower()
            # Simple substring scoring: count how many query words appear in zone name
            words = [w for w in query.split() if len(w) > 2]
            score = sum(1 for w in words if w in name_lower)
            if score > best_score:
                best_score = score
                best = z
        if best and best_score > 0:
            return list(best["coords"])

        return None

    start_c = geocode(origin)
    dest_c  = geocode(dest)
    if not start_c or not dest_c:
        return jsonify({"ok": False, "msg": "Could not geocode one or both locations"}), 400

    path, dist, eta = _fetch_osrm(start_c, dest_c)
    if not path or len(path) < 5:
        path, dist, eta = router.calculate_traffic_route(start_c, dest_c, timeout=TOMTOM_TIMEOUT)
        if path: path = [[float(p[0]), float(p[1])] for p in path]

    if not path or len(path) < 5:
        return jsonify({"ok": False, "msg": "Could not find a route between these locations"}), 400

    str_km  = haversine(start_c[0], start_c[1], dest_c[0], dest_c[1])
    str_eta = max(1, round((str_km / 25) * 60))

    with _lock:
        fleet_id = f"CX-{int(time.time()) % 10000:04d}"
        custom_fleet = {
            "id":               fleet_id,
            "slot":             0,
            "prio":             1,
            "fuel":             100.0,
            "start":            start_c,
            "dest":             dest_c,
            "zone":             dest,
            "start_zone":       origin,
            "route":            path,
            "current_step":     0,
            "progress":         0.0,
            "status":           "Active",
            "color":            "medical",
            "reroutes":         0,
            "swap_cooldown":    0,
            "eta":              eta,
            "dist_km":          dist,
            "straight_km":      str_km,
            "straight_eta":     str_eta,
            "time_saved_mins":  str_eta - eta,
            "road_overhead_pct": max(0, round(((dist - str_km) / max(str_km, 0.1)) * 100)),
            "cur_lat":          path[0][0],
            "cur_lon":          path[0][1],
            "traffic_ratio":    1.0,
            "custom":           True,
        }
        sim["fleets"].append(custom_fleet)
        sim["running"] = True
        _log(f"📍 Custom fleet {fleet_id}: {origin} → {dest} ({dist}km)")

    _persist_state()
    return jsonify({"ok": True, "fleet_id": fleet_id, "dist_km": dist, "eta": eta})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    global _route_cache
    with _lock:
        sim["running"] = False
        sim["deploying"] = False
        sim["fleets"] = []
        sim["frame"] = 0
        sim["stats"] = {"active":0,"delivered":0,"god_mode":0}
        sim["slots_deployed"] = 0
        sim["log"] = []
    try:
        if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
    except Exception:
        pass
    # Re-prefetch routes so next deploy is also instant
    _cache_ready.clear()
    threading.Thread(target=_prefetch_all_routes, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    with _lock:
        fleets = sim["fleets"]
        veh = []
        for f in fleets:
            total = max(len(f["route"])-1, 1)
            pct   = round(f["current_step"] / total * 100)
            veh.append({"type":"Feature",
                "geometry":{"type":"Point","coordinates":[f["cur_lon"],f["cur_lat"]]},
                "properties":{"id":f["id"],"status":f["status"],"color":f.get("color","normal"),
                    "fuel":f["fuel"],"prio":f["prio"],"eta":f["eta"],"zone":f["zone"],
                    "pct":pct,"slot":f.get("slot",1),"custom":f.get("custom",False)}})

        rte = []
        for f in fleets:
            if "Delivered" in f["status"]: continue
            step = f["current_step"]; remain = f["route"][step:]
            stride = max(1, len(remain)//80)
            coords = [[p[1],p[0]] for p in remain[::stride]]
            if len(coords) < 2: continue
            coords[0] = [f["cur_lon"], f["cur_lat"]]
            rte.append({"type":"Feature",
                "geometry":{"type":"LineString","coordinates":coords},
                "properties":{"id":f["id"],"color":f.get("color","normal"),
                    "slot":f.get("slot",1),"custom":f.get("custom",False)}})

        fl = [{"id":f["id"],"slot":f.get("slot",1),"status":f["status"],
               "color":f.get("color","normal"),"prio":f["prio"],"fuel":f["fuel"],
               "eta":f["eta"],"zone":f["zone"],"custom":f.get("custom",False),
               "pct":round(f["current_step"]/max(len(f["route"])-1,1)*100)}
              for f in fleets]

        return jsonify({"running":sim["running"],"deploying":sim["deploying"],
            "frame":sim["frame"],"stats":sim["stats"],
            "slots_deployed":sim["slots_deployed"],"log":sim["log"][:30],
            "vehicles":{"type":"FeatureCollection","features":veh},
            "routes":{"type":"FeatureCollection","features":rte},"fleets":fl})


@app.route("/api/fleet/<fid>")
def api_fleet(fid):
    with _lock:
        f = next((x for x in sim["fleets"] if x["id"]==fid), None)
        if not f: return jsonify({"error":"not found"}), 404
        step, total = f["current_step"], len(f["route"])
        st = max(1, step//60); sr = max(1, (total-step)//60)
        return jsonify({"id":f["id"],"slot":f.get("slot",1),"status":f["status"],
            "color":f.get("color","normal"),"prio":f["prio"],"fuel":f["fuel"],
            "eta":f["eta"],"zone":f["zone"],"start_zone":f.get("start_zone",""),
            "dist_km":f.get("dist_km",0),"reroutes":f["reroutes"],"cur_lat":f["cur_lat"],
            "cur_lon":f["cur_lon"],"dest":f["dest"],"start":f.get("start",[]),
            "pct":round(step/max(total-1,1)*100),"current_step":step,"total_steps":total,
            "route_traveled":[[p[1],p[0]] for p in f["route"][:step+1:st]],
            "route_remaining":[[p[1],p[0]] for p in f["route"][step::sr]],
            "straight_km":f.get("straight_km",0),"straight_eta":f.get("straight_eta",0),
            "time_saved_mins":f.get("time_saved_mins",0),
            "road_overhead_pct":f.get("road_overhead_pct",0),
            "traffic_ratio":f.get("traffic_ratio",1.0),"custom":f.get("custom",False)})


if __name__ == "__main__":
    print("\n" + "═"*50)
    print("  🌐  MumbaiPravaas Command Center")
    print("  →   http://localhost:8080")
    print("═"*50 + "\n")
    print("  ℹ️   Ready — click Deploy Slot 1/5 to begin\n")
    app.run(debug=False, port=8080, threaded=True)
