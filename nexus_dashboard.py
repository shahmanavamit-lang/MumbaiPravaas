import streamlit as st
import pandas as pd
import time
import random
import math
from datetime import datetime
import pydeck as pdk
from marl_router import MARLRouter

# --- PASTE YOUR FREE TOMTOM API KEY HERE ---
TOMTOM_API_KEY = "YLGrbeqrrvkTczVWNRbhtSDBARb2QWYw"

st.set_page_config(layout="wide", page_title="Neural Nexus: Command Center")

st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"], [data-testid="stApp"], .stApp {
        opacity: 1 !important; filter: none !important; background: transparent !important;
    }
    div[data-testid="stAppViewBlockContainer"] { opacity: 1 !important; }
    [data-test-script-state="running"] [data-testid="stAppViewContainer"] { opacity: 1 !important; }
    [aria-busy="true"] { opacity: 1 !important; filter: none !important; }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("🌐 Nexus Route: 50-Fleet Swarm Simulation")

router = MARLRouter(TOMTOM_API_KEY)
center_lat, center_lon = 19.0760, 72.8777

# --- MASSIVE GEOGRAPHIC EXPANSION TO SPREAD 50 CARS ---
MUMBAI_ZONES = [
    {"name": "Andheri Tech Park", "coords": (19.1136, 72.8697)},
    {"name": "Bandra Kurla Complex", "coords": (19.0596, 72.8295)},
    {"name": "Dadar Hub", "coords": (19.0178, 72.8478)},
    {"name": "Powai Valley", "coords": (19.1176, 72.9060)},
    {"name": "Fort Financial", "coords": (18.9400, 72.8352)},
    {"name": "Borivali North", "coords": (19.2307, 72.8567)},
    {"name": "Thane Outskirts", "coords": (19.2183, 72.9780)},
    {"name": "Navi Mumbai Hub", "coords": (19.0770, 72.9986)},
    {"name": "Colaba Port", "coords": (18.9067, 72.8147)},
    {"name": "Chembur East", "coords": (19.0522, 72.8996)}
]

def deploy_swarm(num_fleets=50): # Upped to 50!
    fleets = []
    attempts = 0 
    
    # Increased attempts because spawning 50 takes more random tries
    while len(fleets) < num_fleets and attempts < 150:
        attempts += 1
        start_zone = random.choice(MUMBAI_ZONES)
        dest_zone = random.choice(MUMBAI_ZONES)
        if start_zone['name'] == dest_zone['name']: continue
            
        start = (start_zone['coords'][0] + random.uniform(-0.01, 0.01), start_zone['coords'][1] + random.uniform(-0.01, 0.01))
        dest = (dest_zone['coords'][0] + random.uniform(-0.01, 0.01), dest_zone['coords'][1] + random.uniform(-0.01, 0.01))
        
        path, dist, eta = router.calculate_traffic_route(start, dest)
        
        if path and len(path) > 5:
            pkg = random.choice(["Medical", "Hardware", "Documents", "Perishables"])
            prio = 1 if pkg == "Medical" else random.choice([2, 3])
            fleets.append({
                "id": f"AX-{len(fleets)+1:02d}", "pkg": pkg, "prio": prio,
                "fuel": random.randint(30, 100), "dest": dest, "zone": dest_zone['name'],
                "route": path, "current_step": 0, "progress": 0.0, 
                "status": "Active", "reroutes": 0, "scan_timer": 0,
                "swap_cooldown": 0, "eta": eta, "history": [] 
            })
            
    return fleets

def log_event(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.activity_log.insert(0, f"[{timestamp}] {msg}")

col1, col2, col3 = st.columns([1, 2, 2])
with col1:
    if st.button("🚀 Deploy 50-Fleet Swarm", type="primary"):
        with st.spinner("AI routing 50 vehicles across Mumbai... (This takes a few seconds)"):
            st.session_state.activity_log = []
            st.session_state.fleets = deploy_swarm(50)
            st.session_state.running = True
            log_event("SYSTEM ONLINE: 50 Fleets Deployed via TomTom Live API.")

track_target = "Show All"
if st.session_state.get('running', False):
    fleet_ids = ["Show All"] + [f['id'] for f in st.session_state.fleets]
    with col2:
        track_target = st.selectbox("🎯 Isolate & Track Fleet:", fleet_ids)

map_view, data_view = st.columns([2.5, 1.5])

if st.session_state.get('running', False):

    for i in range(len(st.session_state.fleets)):
        for j in range(i + 1, len(st.session_state.fleets)):
            f1, f2 = st.session_state.fleets[i], st.session_state.fleets[j]
            
            if "Delivered" in f1['status'] or "Delivered" in f2['status']: continue
            if "GOD MODE" in f1['status'] or "GOD MODE" in f2['status']: continue
            if f1['swap_cooldown'] > 0 or f2['swap_cooldown'] > 0: continue
            
            action, msg = router.evaluate_marl_policy(f1, f2)
            
            if action == "MERGE":
                f1['history'].append([[lon, lat] for lat, lon in f1['route'][f1['current_step']:]])
                f1['pkg'] = f"{f1['pkg']} + {f2['pkg']}"
                f1['prio'] = min(f1['prio'], f2['prio'])
                f1['status'] = "Consolidated 📦"
                
                f2['history'] = [] 
                
                new_zone = random.choice([z for z in MUMBAI_ZONES if z['name'] != f2['zone']])
                f2['dest'] = (new_zone['coords'][0] + random.uniform(-0.005, 0.005), new_zone['coords'][1] + random.uniform(-0.005, 0.005))
                f2['zone'] = new_zone['name']
                f2['pkg'] = random.choice(["Hardware", "Perishables"])
                
                # UPDATED STATUS TO SHOW REASON
                f2['status'] = "Reassigned (Merged) 🔄" 
                
                f1['route'], _, f1['eta'] = router.calculate_traffic_route(f1['route'][f1['current_step']], f1['dest'])
                f2['route'], _, f2['eta'] = router.calculate_traffic_route(f2['route'][f2['current_step']], f2['dest'])
                
                f1['current_step'], f1['progress'], f2['current_step'], f2['progress'] = 0, 0.0, 0, 0.0
                f1['reroutes'] += 1; f2['reroutes'] += 1
                f1['swap_cooldown'] = 100; f2['swap_cooldown'] = 100
                log_event(msg)
                
            elif action == "FUEL_EXCHANGE":
                f1['history'] = []
                f2['history'] = []
                
                f1['dest'], f2['dest'] = f2['dest'], f1['dest']
                f1['zone'], f2['zone'] = f2['zone'], f1['zone']
                f1['pkg'], f2['pkg'] = f2['pkg'], f1['pkg']
                f1['prio'], f2['prio'] = f2['prio'], f1['prio']
                
                f1['route'], _, f1['eta'] = router.calculate_traffic_route(f1['route'][f1['current_step']], f1['dest'])
                f2['route'], _, f2['eta'] = router.calculate_traffic_route(f2['route'][f2['current_step']], f2['dest'])
                
                f1['current_step'], f1['progress'], f2['current_step'], f2['progress'] = 0, 0.0, 0, 0.0
                
                # UPDATED STATUS TO SHOW REASON
                f1['status'], f2['status'] = "Swapped (Fuel) 🔄", "Swapped (Fuel) 🔄"
                
                f1['reroutes'] += 1; f2['reroutes'] += 1
                f1['swap_cooldown'] = 50; f2['swap_cooldown'] = 50
                log_event(msg)

    live_data = []
    zomato_routes = []
    ghost_routes = []
    vehicles = []
    dest_pins = []

    for f in st.session_state.fleets:
        if f['status'] == "Delivered": continue
        if f['swap_cooldown'] > 0: f['swap_cooldown'] -= 1
        
        if f['pkg'] == "Medical" and "GOD MODE" not in f['status']:
            if f['eta'] > 6 and f['fuel'] < 45:
                f['status'] = "GOD MODE 🚨"
                log_event(f"SLA EMERGENCY: {f['id']} (Medical) at risk! AI engaged GOD MODE (Sirens Active, Speed x2, Fuel Ignored).")

        if "GOD MODE" in f['status']:
            move_speed = 0.35 
            f['fuel'] = max(20.0, f['fuel']) 
        else:
            move_speed = 0.15 if f['prio'] == 1 else 0.08 
            if f['fuel'] < 20: 
                move_speed = 0.04
                if "Swapped" not in f['status'] and "Consol" not in f['status']: f['status'] = "Low Fuel"
            
        if f['current_step'] < len(f['route']) - 1:
            f['progress'] += move_speed
            
            if f['progress'] >= 1.0:
                f['current_step'] += 1
                f['progress'] = 0.0
                if "GOD MODE" not in f['status']:
                    f['fuel'] = max(0, round(f['fuel'] - 0.5, 1))
                f['scan_timer'] += 1 
                
                if f['scan_timer'] >= 10 and "GOD MODE" not in f['status']:
                    current_loc = f['route'][f['current_step']]
                    new_path, dist, new_eta = router.calculate_traffic_route(current_loc, f['dest'])
                    current_rem_nodes = len(f['route']) - f['current_step']
                    
                    if new_path and len(new_path) > 2 and (f['eta'] - new_eta) >= 2:
                        if abs(len(new_path) - current_rem_nodes) > 5: 
                            old_eta = f['eta']
                            f['history'].append([[lon, lat] for lat, lon in f['route'][f['current_step']:]])
                            f['route'] = new_path
                            f['eta'] = new_eta
                            f['current_step'] = 0
                            f['reroutes'] += 1
                            log_event(f"DYNAMIC REROUTE: {f['id']} found a faster path saving {old_eta - new_eta} mins!")
                    f['scan_timer'] = 0
            
            if f['current_step'] < len(f['route']) - 1:
                lat1, lon1 = f['route'][f['current_step']]
                lat2, lon2 = f['route'][f['current_step'] + 1]
                current_lat = lat1 + (lat2 - lat1) * f['progress']
                current_lon = lon1 + (lon2 - lon1) * f['progress']
                remaining_path = [[current_lon, current_lat]] + [[lon, lat] for lat, lon in f['route'][f['current_step']+1:]]
            else:
                current_lat, current_lon = f['dest']
                remaining_path = []
                
            is_tracked = (track_target == "Show All" or track_target == f['id'])
            opacity = 255 if is_tracked else 20 
            
            route_width = 4 if (is_tracked and track_target != "Show All") else 2 # Thinner lines for 50 fleets
            route_color = [255, 50, 50, opacity] if f['prio'] == 1 else [50, 150, 255, opacity]
            veh_fill = [255, 255, 255, opacity]
            veh_radius = 40
            
            if "GOD MODE" in f['status']:
                route_color = [0, 255, 255, opacity] 
                route_width = 6 if is_tracked else 4
                veh_fill = [0, 255, 255, opacity] 
                veh_radius = 70 
            
            zomato_routes.append({"unit_id": f['id'], "path": remaining_path, "color": route_color, "width": route_width})
            
            if is_tracked and track_target != "Show All":
                for old_path in f['history']:
                    ghost_routes.append({"path": old_path, "color": [0, 255, 50, 255], "width": 4})
            
            vehicles.append({
                "unit_id": f['id'], "position": [current_lon, current_lat], 
                "fill": veh_fill, "outline": [0, 0, 0, opacity], "radius": veh_radius 
            })
            dest_pins.append({
                "unit_id": f['id'] + " Dropoff", "position": [f['dest'][1], f['dest'][0]], "icon": "📍", "color": [255, 255, 255, opacity]
            })
            
        else:
            f['status'] = "Delivered"
            f['eta'] = 0

        live_data.append({
            "Unit": f['id'], "Zone": f['zone'], "ETA": f"{f['eta']} min", 
            "Fuel": f"{f['fuel']}%", "Cargo": f['pkg'], "State": f['status']
        })

    traffic_tile_layer = pdk.Layer("TileLayer", id="global_traffic", data=f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{{z}}/{{x}}/{{y}}.png?key={TOMTOM_API_KEY}", opacity=0.7)
    ghost_layer = pdk.Layer("PathLayer", id="ghosts", data=ghost_routes, get_path="path", get_color="color", get_width="width", width_min_pixels=3)
    route_layer = pdk.Layer("PathLayer", id="routes", data=zomato_routes, get_path="path", get_color="color", get_width="width", width_min_pixels=2, pickable=True, auto_highlight=True)
    vehicle_layer = pdk.Layer("ScatterplotLayer", id="cars", data=vehicles, get_position="position", get_fill_color="fill", get_line_color="outline", stroked=True, line_width_min_pixels=2, get_radius="radius", radius_min_pixels=6, pickable=True)
    dest_layer = pdk.Layer("TextLayer", id="destinations", data=dest_pins, get_position="position", get_text="icon", get_size=25, get_color="color", get_alignment_baseline="'bottom'", pickable=True)

    if track_target != "Show All":
        tracked_f = next(f for f in st.session_state.fleets if f['id'] == track_target)
        cam_lat, cam_lon = tracked_f['route'][tracked_f['current_step']]
        view_state = pdk.ViewState(latitude=cam_lat, longitude=cam_lon, zoom=14.5, pitch=50)
    else:
        # Pulled the camera back slightly to fit all of Mumbai
        view_state = pdk.ViewState(latitude=19.1000, longitude=72.9000, zoom=10.5, pitch=40)
    
    with map_view:
        st.pydeck_chart(pdk.Deck(layers=[traffic_tile_layer, ghost_layer, route_layer, dest_layer, vehicle_layer], initial_view_state=view_state, map_style='dark', tooltip={"text": "{unit_id}"}))
    
    with data_view:
        st.subheader("📋 Swarm Telemetry")
        st.dataframe(pd.DataFrame(live_data), height=300, use_container_width=True)
        
        st.subheader("📡 Command Center Log")
        log_container = st.container(height=230)
        with log_container:
            for log_msg in st.session_state.activity_log:
                if "SLA EMERGENCY" in log_msg:
                    st.markdown(f"**<span style='color:#00FFFF;'>{log_msg}</span>**", unsafe_allow_html=True) 
                elif "FUEL EFFICIENCY" in log_msg:
                    st.markdown(f"**<span style='color:#FF007F;'>{log_msg}</span>**", unsafe_allow_html=True)
                elif "CONSOLIDATION" in log_msg:
                    st.markdown(f"**<span style='color:#00BFFF;'>{log_msg}</span>**", unsafe_allow_html=True)
                elif "REROUTE" in log_msg:
                    st.markdown(f"**<span style='color:#00FFAA;'>{log_msg}</span>**", unsafe_allow_html=True)
                else:
                    st.markdown(f"<span style='color:#CCCCCC;'>{log_msg}</span>", unsafe_allow_html=True)

    time.sleep(0.1)
    st.rerun()