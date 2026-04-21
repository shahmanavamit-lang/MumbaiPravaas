import requests

class MARLRouter:
    def __init__(self, api_key):
        self.api_key = api_key

    def calculate_traffic_route(self, start_coords, end_coords):
        """Calls TomTom API for live traffic-aware routing & ETA."""
        start = f"{start_coords[0]},{start_coords[1]}"
        end = f"{end_coords[0]},{end_coords[1]}"
        
        url = f"https://api.tomtom.com/routing/1/calculateRoute/{start}:{end}/json"
        params = {'key': self.api_key, 'traffic': 'true', 'travelMode': 'car'}
        
        try:
            response = requests.get(url, params=params, timeout=5)
            data = response.json()
            
            if 'routes' in data and len(data['routes']) > 0:
                pts = data['routes'][0]['legs'][0]['points']
                path_coords = [[pt['latitude'], pt['longitude']] for pt in pts]
                dist_km = data['routes'][0]['summary']['lengthInMeters'] / 1000
                eta_mins = round(data['routes'][0]['summary']['travelTimeInSeconds'] / 60)
                return path_coords, dist_km, eta_mins
            else:
                return None, 0, 0
        except Exception as e:
            return None, 0, 0

    def evaluate_marl_policy(self, f1, f2):
        """
        The core AI Policy Engine. Scans two nearby vehicles and decides the optimal action.
        Returns: (Action_Type, Notification_Message) or (None, None)
        """
        # 1. Proximity Check (Are they close enough to interact?)
        lat_diff = abs(f1['route'][f1['current_step']][0] - f2['route'][f2['current_step']][0])
        lon_diff = abs(f1['route'][f1['current_step']][1] - f2['route'][f2['current_step']][1])
        
        if lat_diff > 0.015 or lon_diff > 0.015:
            return None, None

        # 2. CARPOOLING LOGIC (Are they going to the same area?)
        dest_lat_diff = abs(f1['dest'][0] - f2['dest'][0])
        dest_lon_diff = abs(f1['dest'][1] - f2['dest'][1])
        if dest_lat_diff < 0.02 and dest_lon_diff < 0.02:
            if "+" not in f1['pkg'] and "+" not in f2['pkg']: # Prevent endless merging
                return "MERGE", f"MARL CONSOLIDATION: {f1['id']} absorbed {f2['id']}'s cargo for optimal delivery! {f2['id']} freed for reassignment."

        # 3. THE NEW STRICT FUEL EFFICIENCY RULE
        f1_rem_nodes = len(f1['route']) - f1['current_step']
        f2_rem_nodes = len(f2['route']) - f2['current_step']

        # Scenario A: F1 has low fuel and a long route, F2 has high fuel and a short route.
        if f1['fuel'] < 25 and f1_rem_nodes > f2_rem_nodes + 10 and f2['fuel'] > 60:
            return "FUEL_EXCHANGE", f"FUEL EFFICIENCY EXCHANGE: {f2['id']} (High Fuel) took over {f1['id']}'s long route. {f1['id']} given short route to save fuel!"
            
        # Scenario B: F2 has low fuel and a long route, F1 has high fuel and a short route.
        elif f2['fuel'] < 25 and f2_rem_nodes > f1_rem_nodes + 10 and f1['fuel'] > 60:
            return "FUEL_EXCHANGE", f"FUEL EFFICIENCY EXCHANGE: {f1['id']} (High Fuel) took over {f2['id']}'s long route. {f2['id']} given short route to save fuel!"

        # No optimal action found
        return None, None