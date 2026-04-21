import random
import osmnx as ox

class CityEnvironment:
    def __init__(self, location="Andheri, Mumbai, India", radius=1200):
        self.location = location
        self.radius = radius

    def download_map(self):
        """Downloads the map graph using OSMnx APIs."""
        try:
            G = ox.graph_from_address(self.location, dist=self.radius, network_type='drive')
            G = ox.add_edge_speeds(G)
            G = ox.add_edge_travel_times(G)
            return G
        except Exception as e:
            return None

    def apply_dynamic_traffic(self, G):
        """Simulates traffic constraints (Red, Orange, Green)."""
        for u, v, k, data in G.edges(data=True, keys=True):
            rand = random.random()
            if rand < 0.15: # 15% Heavy Traffic
                data['travel_time'] = data.get('travel_time', 1) * 5.0
                data['color'] = "#FF4B4B" # Red
            elif rand < 0.40: # 25% Moderate
                data['travel_time'] = data.get('travel_time', 1) * 2.5
                data['color'] = "#FFA500" # Orange
            else: # 60% Clear
                data['travel_time'] = data.get('travel_time', 1) * 1.0
                data['color'] = "#2ECC71" # Green
        return G