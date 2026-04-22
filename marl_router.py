import requests

class MARLRouter:
    def __init__(self, api_key):
        self.api_key = api_key

    def calculate_traffic_route(self, start_coords, end_coords, timeout=6):
        """
        Calls TomTom Routing API for live traffic-aware routing & ETA.
        Returns (path_as_list_of_[lat,lon], dist_km, eta_mins) or (None, 0, 0) on failure.
        """
        start = f"{start_coords[0]},{start_coords[1]}"
        end   = f"{end_coords[0]},{end_coords[1]}"
        url   = f"https://api.tomtom.com/routing/1/calculateRoute/{start}:{end}/json"
        params = {
            'key':        self.api_key,
            'traffic':    'true',
            'travelMode': 'car',
        }
        try:
            response = requests.get(url, params=params, timeout=timeout)
            data     = response.json()
            if 'routes' in data and len(data['routes']) > 0:
                pts      = data['routes'][0]['legs'][0]['points']
                path     = [[pt['latitude'], pt['longitude']] for pt in pts]
                dist_km  = data['routes'][0]['summary']['lengthInMeters'] / 1000
                eta_mins = round(data['routes'][0]['summary']['travelTimeInSeconds'] / 60)
                return path, dist_km, eta_mins
        except Exception:
            pass
        return None, 0, 0

    def calculate_osrm_route(self, start_coords, end_coords, timeout=8):
        """
        Fallback: calls the public OSRM demo server for real road-following routing.
        Free, no API key required. Returns real road geometry — no ocean/building shortcuts.
        Returns (path_as_list_of_[lat,lon], dist_km, eta_mins) or (None, 0, 0) on failure.
        """
        lon1, lat1 = start_coords[1], start_coords[0]
        lon2, lat2 = end_coords[1],   end_coords[0]
        url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{lon1},{lat1};{lon2},{lat2}"
        )
        params = {
            'overview':   'full',
            'geometries': 'geojson',
            'steps':      'false',
        }
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            data = resp.json()
            if data.get('code') == 'Ok' and data.get('routes'):
                # OSRM returns [[lon, lat], ...] — convert to [[lat, lon]]
                coords   = data['routes'][0]['geometry']['coordinates']
                path     = [[c[1], c[0]] for c in coords]
                dist_km  = round(data['routes'][0]['distance'] / 1000, 1)
                eta_mins = max(1, round(data['routes'][0]['duration'] / 60))
                return path, dist_km, eta_mins
        except Exception:
            pass
        return None, 0, 0

    def evaluate_marl_policy(self, f1, f2):
        """
        Core AI Policy Engine: scans two nearby vehicles and decides the optimal action.
        Returns (action_str, log_message) or (None, None) if no action needed.
        """
        # Bounds-safe step access
        step1 = min(f1['current_step'], len(f1['route']) - 1)
        step2 = min(f2['current_step'], len(f2['route']) - 1)

        # 1. Proximity check — only interact if vehicles are close
        lat_diff = abs(f1['route'][step1][0] - f2['route'][step2][0])
        lon_diff = abs(f1['route'][step1][1] - f2['route'][step2][1])
        if lat_diff > 0.015 or lon_diff > 0.015:
            return None, None

        # 2. CONSOLIDATION: same destination → merge the two loads
        dest_lat_diff = abs(f1['dest'][0] - f2['dest'][0])
        dest_lon_diff = abs(f1['dest'][1] - f2['dest'][1])
        if dest_lat_diff < 0.02 and dest_lon_diff < 0.02:
            # swap_cooldown=100 already prevents re-merging of same pair
            return (
                "MERGE",
                f"MARL CONSOLIDATION: {f1['id']} absorbed {f2['id']}'s load — "
                f"both heading to {f1['zone']}. {f2['id']} reassigned."
            )

        # 3. FUEL EXCHANGE: low-fuel vehicle gets a shorter route
        rem1 = len(f1['route']) - step1
        rem2 = len(f2['route']) - step2
        if f1['fuel'] < 25 and rem1 > rem2 + 10 and f2['fuel'] > 60:
            return (
                "FUEL_EXCHANGE",
                f"FUEL EFFICIENCY: {f2['id']} (High Fuel) takes {f1['id']}'s long route. "
                f"{f1['id']} given shorter delivery to prevent fuel failure."
            )
        if f2['fuel'] < 25 and rem2 > rem1 + 10 and f1['fuel'] > 60:
            return (
                "FUEL_EXCHANGE",
                f"FUEL EFFICIENCY: {f1['id']} (High Fuel) takes {f2['id']}'s long route. "
                f"{f2['id']} given shorter delivery to prevent fuel failure."
            )

        return None, None