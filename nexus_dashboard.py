import streamlit as st
import pandas as pd
import time
import random
import math
from datetime import datetime
import pydeck as pdk
from concurrent.futures import ThreadPoolExecutor, as_completed
from marl_router import MARLRouter

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOMTOM_API_KEY   = " "
NUM_FLEETS       = 50
MOVE_SPEED_PRIO  = 0.40   # steps/frame for Priority-1 (Medical)
MOVE_SPEED_NORM  = 0.22   # steps/frame for normal cargo
MOVE_SPEED_LOW   = 0.10   # steps/frame when low fuel
MOVE_SPEED_GOD   = 0.65   # steps/frame for GOD-MODE
FRAME_SLEEP      = 0.12   # seconds between animation frames
MAX_WORKERS      = 10     # parallel TomTom API threads at deploy time

st.set_page_config(layout="wide", page_title="Neural Nexus: Command Center", page_icon="🌐")

# ─── CSS – suppress Streamlit flicker on heavy reruns ────────────────────────
st.markdown("""
<style>
/* Keep app fully opaque while Streamlit re-renders */
[data-testid="stAppViewContainer"],
[data-testid="stApp"], .stApp,
[aria-busy="true"],
[data-test-script-state="running"] [data-testid="stAppViewContainer"] {
    opacity: 1 !important;
    filter: none !important;
    background: transparent !important;
}
div[data-testid="stAppViewBlockContainer"] { opacity: 1 !important; }

/* Compact metric cards */
[data-testid="metric-container"] {
    background: #0e1117;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 6px 10px;
}
</style>
""", unsafe_allow_html=True)

# ─── INIT ─────────────────────────────────────────────────────────────────────
st.title("🌐 Neural Nexus — 50-Fleet MARL Swarm Simulation")

@st.cache_resource
def get_router():
    return MARLRouter(TOMTOM_API_KEY)

router = get_router()

MUMBAI_ZONES = [
    {"name": "Andheri Tech Park",    "coords": (19.1136, 72.8697)},
    {"name": "Bandra Kurla Complex", "coords": (19.0596, 72.8295)},
    {"name": "Dadar Hub",            "coords": (19.0178, 72.8478)},
    {"name": "Powai Valley",         "coords": (19.1176, 72.9060)},
    {"name": "Fort Financial",       "coords": (18.9400, 72.8352)},
    {"name": "Borivali North",       "coords": (19.2307, 72.8567)},
    {"name": "Thane Outskirts",      "coords": (19.2183, 72.9780)},
    {"name": "Navi Mumbai Hub",      "coords": (19.0770, 72.9986)},
    {"name": "Colaba Port",          "coords": (18.9067, 72.8147)},
    {"name": "Chembur East",         "coords": (19.0522, 72.8996)},
]

# ─── PARALLEL ROUTE FETCHER ───────────────────────────────────────────────────
def fetch_one_route(task):
    """Runs in a thread – fetches one TomTom route."""
    idx, start, dest = task
    path, dist, eta = router.calculate_traffic_route(start, dest)
    return idx, path, dist, eta


def _build_route_candidates(num_fleets):
    """Create a diverse list of origin/destination pairs for deploy."""
    candidates = []
    zone_order = MUMBAI_ZONES.copy()
    random.shuffle(zone_order)

    while len(candidates) < num_fleets * 2:
        for sz in zone_order:
            dz = random.choice([z for z in MUMBAI_ZONES if z["name"] != sz["name"]])
            s = (sz["coords"][0] + random.uniform(-0.015, 0.015),
                 sz["coords"][1] + random.uniform(-0.015, 0.015))
            d = (dz["coords"][0] + random.uniform(-0.015, 0.015),
                 dz["coords"][1] + random.uniform(-0.015, 0.015))
            candidates.append((sz, dz, s, d))
            if len(candidates) >= num_fleets * 2:
                break
        random.shuffle(zone_order)
    return candidates


def deploy_swarm(num_fleets=NUM_FLEETS):
    """
    Build candidate list first (no API), then fetch all routes in parallel.
    This cuts deploy time from ~50s sequential → ~5-8s parallel.
    """
    candidates = _build_route_candidates(num_fleets)

    # Parallel API calls
    tasks   = [(i, c[2], c[3]) for i, c in enumerate(candidates)]
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one_route, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            idx, path, dist, eta = fut.result()
            results[idx] = (path, dist, eta)

    fleets = []
    for i, (sz, dz, s, d) in enumerate(candidates):
        if len(fleets) >= num_fleets:
            break
        path, dist, eta = results.get(i, (None, 0, 0))
        if path and len(path) > 5:
            pkg  = random.choice(["Medical", "Hardware", "Documents", "Perishables"])
            prio = 1 if pkg == "Medical" else random.choice([2, 3])
            fleets.append({
                "id":           f"AX-{len(fleets)+1:02d}",
                "pkg":          pkg,
                "prio":         prio,
                "fuel":         random.randint(30, 100),
                "dest":         d,
                "zone":         dz["name"],
                "route":        path,
                "current_step": 0,
                "progress":     0.0,
                "status":       "Active",
                "reroutes":     0,
                "scan_timer":   0,
                "swap_cooldown":0,
                "eta":          eta,
                "history":      [],
            })
    return fleets

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def log_event(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.activity_log.insert(0, f"[{ts}] {msg}")
    if len(st.session_state.activity_log) > 60:
        st.session_state.activity_log = st.session_state.activity_log[:60]

# ─── CONTROL BAR ──────────────────────────────────────────────────────────────
ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1, 2, 2])

with ctrl_col1:
    deploy_btn = st.button("🚀 Deploy 50-Fleet Swarm", type="primary")

track_target = "Show All"
if st.session_state.get("running", False):
    fleet_ids = ["Show All"] + [f["id"] for f in st.session_state.fleets]
    with ctrl_col2:
        track_target = st.selectbox("🎯 Isolate & Track Fleet:", fleet_ids)

# ─── DEPLOY ACTION ────────────────────────────────────────────────────────────
if deploy_btn:
    with st.spinner(
        f"⚡ AI routing {NUM_FLEETS} vehicles in parallel… (~5-10 s)"
    ):
        st.session_state.activity_log = []
        st.session_state.fleets       = deploy_swarm(NUM_FLEETS)
        st.session_state.running      = True
        log_event(
            f"SYSTEM ONLINE: {len(st.session_state.fleets)} Fleets deployed via TomTom Live API."
        )

# ─── MAIN LAYOUT PLACEHOLDERS (created ONCE, updated in-place) ────────────────
map_col, data_col = st.columns([2.5, 1.5])

with map_col:
    map_placeholder = st.empty()

with data_col:
    metrics_placeholder = st.empty()
    st.subheader("📋 Swarm Telemetry")
    table_placeholder   = st.empty()
    st.subheader("📡 Command Log")
    log_placeholder     = st.empty()

# ─── SIMULATION LOOP ──────────────────────────────────────────────────────────
# Uses while-True + st.empty() → NO st.rerun() → NO page-level DOM rebuild
# → eliminates the black-flash caused by the TileLayer reloading every frame.

if st.session_state.get("running", False):

    while True:
        fleets = st.session_state.fleets

        # 1. MARL POLICY (agent-to-agent decisions, no new API calls)
        for i in range(len(fleets)):
            for j in range(i + 1, len(fleets)):
                f1, f2 = fleets[i], fleets[j]
                if "Delivered" in f1["status"] or "Delivered" in f2["status"]:
                    continue
                if "GOD MODE" in f1["status"] or "GOD MODE" in f2["status"]:
                    continue
                if f1["swap_cooldown"] > 0 or f2["swap_cooldown"] > 0:
                    continue

                action, msg = router.evaluate_marl_policy(f1, f2)

                if action == "MERGE":
                    f1["history"].append([[lon, lat] for lat, lon in f1["route"][f1["current_step"]:]])
                    f1["pkg"]    = f"{f1['pkg']} + {f2['pkg']}"
                    f1["prio"]   = min(f1["prio"], f2["prio"])
                    f1["status"] = "Consolidated 📦"

                    new_zone = random.choice([z for z in MUMBAI_ZONES if z["name"] != f2["zone"]])
                    f2["dest"]   = (new_zone["coords"][0] + random.uniform(-0.005, 0.005),
                                    new_zone["coords"][1] + random.uniform(-0.005, 0.005))
                    f2["zone"]   = new_zone["name"]
                    f2["pkg"]    = random.choice(["Hardware", "Perishables"])
                    f2["status"] = "Reassigned (Merged) 🔄"
                    f2["history"] = []

                    # Reverse route instead of new API call – fast fallback
                    merged_step    = min(f1["current_step"], len(f1["route"]) - 1)
                    f1["route"]   = f1["route"][merged_step:]
                    f1["current_step"] = 0; f1["progress"] = 0.0
                    f2["current_step"] = 0; f2["progress"] = 0.0
                    f1["reroutes"] += 1; f2["reroutes"] += 1
                    f1["swap_cooldown"] = 100; f2["swap_cooldown"] = 100
                    log_event(msg)

                elif action == "FUEL_EXCHANGE":
                    f1["dest"],  f2["dest"]  = f2["dest"],  f1["dest"]
                    f1["zone"],  f2["zone"]  = f2["zone"],  f1["zone"]
                    f1["pkg"],   f2["pkg"]   = f2["pkg"],   f1["pkg"]
                    f1["prio"],  f2["prio"]  = f2["prio"],  f1["prio"]
                    f1["history"] = []; f2["history"] = []

                    # Swap remaining routes instead of new API call
                    rem1 = f1["route"][f1["current_step"]:]
                    rem2 = f2["route"][f2["current_step"]:]
                    f1["route"] = rem2; f2["route"] = rem1
                    f1["current_step"] = 0; f1["progress"] = 0.0
                    f2["current_step"] = 0; f2["progress"] = 0.0
                    f1["status"] = "Swapped (Fuel) 🔄"; f2["status"] = "Swapped (Fuel) 🔄"
                    f1["reroutes"] += 1; f2["reroutes"] += 1
                    f1["swap_cooldown"] = 50; f2["swap_cooldown"] = 50
                    log_event(msg)

        # 2. ADVANCE EACH FLEET (pure in-memory, no API calls)
        live_data    = []
        zomato_routes = []
        ghost_routes  = []
        vehicles      = []
        dest_pins     = []

        active = delivered = god_mode = 0

        for f in fleets:
            if f["swap_cooldown"] > 0:
                f["swap_cooldown"] -= 1

            # GOD MODE trigger
            if f["pkg"] == "Medical" and "GOD MODE" not in f["status"]:
                if f["eta"] > 6 and f["fuel"] < 45:
                    f["status"] = "GOD MODE 🚨"
                    log_event(
                        f"SLA EMERGENCY: {f['id']} (Medical) at risk! GOD MODE activated."
                    )

            if "GOD MODE" in f["status"]:
                speed = MOVE_SPEED_GOD
                f["fuel"] = max(20.0, f["fuel"])
                god_mode += 1
            elif f["fuel"] < 20:
                speed = MOVE_SPEED_LOW
                if "Swapped" not in f["status"] and "Consol" not in f["status"]:
                    f["status"] = "Low Fuel ⛽"
            else:
                speed = MOVE_SPEED_PRIO if f["prio"] == 1 else MOVE_SPEED_NORM

            is_tracked = (track_target == "Show All" or track_target == f["id"])
            opacity    = 255 if is_tracked else 40

            if f["status"] == "Delivered":
                delivered += 1
                live_data.append({
                    "Unit": f["id"], "Zone": f["zone"], "ETA": "✅",
                    "Fuel": f"{f['fuel']}%", "Cargo": f["pkg"], "State": f["status"],
                })
                continue

            active += 1

            if f["current_step"] < len(f["route"]) - 1:
                f["progress"] += speed

                while f["progress"] >= 1.0 and f["current_step"] < len(f["route"]) - 1:
                    f["current_step"] += 1
                    f["progress"] -= 1.0
                    if "GOD MODE" not in f["status"]:
                        f["fuel"] = max(0, round(f["fuel"] - 0.3, 1))
                    # Reduce ETA as we move (no API call, just approximate)
                    if f["eta"] > 0:
                        f["eta"] = max(0, f["eta"] - 1)
                    f["scan_timer"] += 1

                if f["current_step"] < len(f["route"]) - 1:
                    lat1, lon1 = f["route"][f["current_step"]]
                    lat2, lon2 = f["route"][f["current_step"] + 1]
                    t = min(f["progress"], 1.0)
                    cur_lat = lat1 + (lat2 - lat1) * t
                    cur_lon = lon1 + (lon2 - lon1) * t
                    remaining = (
                        [[cur_lon, cur_lat]]
                        + [[lon, lat] for lat, lon in f["route"][f["current_step"] + 1:]]
                    )
                else:
                    cur_lat, cur_lon = f["dest"]
                    remaining = []

                # Route colour
                if "GOD MODE" in f["status"]:
                    r_col = [0, 255, 255, opacity]
                    v_fill = [0, 255, 255, opacity]
                    r_w = 6 if is_tracked else 4
                    v_r = 70
                elif f["prio"] == 1:
                    r_col = [255, 60, 60, opacity]
                    v_fill = [255, 200, 200, opacity]
                    r_w = 4 if is_tracked else 2
                    v_r = 50
                else:
                    r_col = [50, 160, 255, opacity]
                    v_fill = [200, 220, 255, opacity]
                    r_w = 3 if is_tracked else 2
                    v_r = 40

                if remaining:
                    zomato_routes.append({
                        "unit_id": f["id"], "path": remaining,
                        "color": r_col, "width": r_w,
                    })

                if is_tracked and track_target != "Show All":
                    for old in f["history"]:
                        ghost_routes.append({"path": old, "color": [0, 255, 80, 200], "width": 3})

                vehicles.append({
                    "unit_id":  f["id"],
                    "position": [cur_lon, cur_lat],
                    "fill":     v_fill,
                    "outline":  [0, 0, 0, opacity],
                    "radius":   v_r,
                })
                dest_pins.append({
                    "unit_id":  f["id"] + " Dropoff",
                    "position": [f["dest"][1], f["dest"][0]],
                    "icon":     "📍",
                    "color":    [255, 255, 255, opacity],
                })

            else:
                f["status"] = "Delivered"
                f["eta"]    = 0
                delivered  += 1

            live_data.append({
                "Unit":  f["id"],
                "Zone":  f["zone"],
                "ETA":   f"{f['eta']} min",
                "Fuel":  f"{f['fuel']}%",
                "Cargo": f["pkg"],
                "State": f["status"],
            })

        # 3. CAMERA
        if track_target != "Show All":
            try:
                tf = next(f for f in fleets if f["id"] == track_target)
                step = min(tf["current_step"], len(tf["route"]) - 1)
                cam_lat, cam_lon = tf["route"][step]
                view_state = pdk.ViewState(latitude=cam_lat, longitude=cam_lon, zoom=14.5, pitch=50)
            except StopIteration:
                view_state = pdk.ViewState(latitude=19.10, longitude=72.90, zoom=10.8, pitch=40)
        else:
            view_state = pdk.ViewState(latitude=19.10, longitude=72.90, zoom=10.8, pitch=40)

        # 4. BUILD LAYERS (no TileLayer → no tile reload → no black flicker)
        layers = []
        if ghost_routes:
            layers.append(pdk.Layer(
                "PathLayer", id="ghosts", data=ghost_routes,
                get_path="path", get_color="color", get_width="width",
                width_min_pixels=2,
            ))
        if zomato_routes:
            layers.append(pdk.Layer(
                "PathLayer", id="routes", data=zomato_routes,
                get_path="path", get_color="color", get_width="width",
                width_min_pixels=2, pickable=True, auto_highlight=True,
            ))
        if dest_pins:
            layers.append(pdk.Layer(
                "TextLayer", id="destinations", data=dest_pins,
                get_position="position", get_text="icon", get_size=22,
                get_color="color", get_alignment_baseline="'bottom'",
            ))
        if vehicles:
            layers.append(pdk.Layer(
                "ScatterplotLayer", id="cars", data=vehicles,
                get_position="position", get_fill_color="fill",
                get_line_color="outline", stroked=True,
                line_width_min_pixels=2, get_radius="radius",
                radius_min_pixels=5, pickable=True,
            ))

        deck = pdk.Deck(
            layers=layers,
            initial_view_state=view_state,
            map_style="mapbox://styles/mapbox/dark-v10",
            tooltip={"text": "{unit_id}"},
        )

        # 5. RENDER – update placeholders IN-PLACE (no page rebuild, no flicker)
        map_placeholder.pydeck_chart(deck, use_container_width=True)

        # Metrics
        with metrics_placeholder.container():
            m1, m2, m3 = st.columns(3)
            m1.metric("🟢 Active",    active)
            m2.metric("✅ Delivered", delivered)
            m3.metric("🚨 GOD MODE",  god_mode)

        # Table – show only active fleets (reduces DOM size)
        df = pd.DataFrame(live_data)
        table_placeholder.dataframe(df, height=280, use_container_width=True)

        # Log
        with log_placeholder.container():
            for msg in st.session_state.activity_log[:25]:
                if "SLA EMERGENCY" in msg or "GOD MODE" in msg:
                    st.markdown(f"**<span style='color:#00FFFF;'>{msg}</span>**", unsafe_allow_html=True)
                elif "FUEL EFFICIENCY" in msg:
                    st.markdown(f"**<span style='color:#FF007F;'>{msg}</span>**", unsafe_allow_html=True)
                elif "CONSOLIDATION" in msg or "MERGE" in msg:
                    st.markdown(f"**<span style='color:#00BFFF;'>{msg}</span>**", unsafe_allow_html=True)
                elif "REROUTE" in msg:
                    st.markdown(f"**<span style='color:#00FFAA;'>{msg}</span>**", unsafe_allow_html=True)
                else:
                    st.markdown(f"<span style='color:#AAAAAA;'>{msg}</span>", unsafe_allow_html=True)

        time.sleep(FRAME_SLEEP)
