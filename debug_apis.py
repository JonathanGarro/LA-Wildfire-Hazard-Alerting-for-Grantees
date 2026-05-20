"""
debug_apis.py — probe calfire service schema + find working perimeter source
"""
import requests, json

HEADERS = {"User-Agent": "HewlettFoundationHazardCheck, data@hewlett.org"}

def test(label, url):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {url[:90]}")
    print(f"{'='*60}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        print(f"  http status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            features = data.get("features", [])
            print(f"  feature count: {len(features)}")
            if features:
                print(f"  first feature properties:")
                print(json.dumps(features[0].get("properties", {}), indent=4)[:1000])
            else:
                print(f"  full response: {json.dumps(data, indent=2)[:600]}")
        else:
            print(f"  error: {r.text[:300]}")
    except Exception as e:
        print(f"  exception: {e}")

# --- probe calfire service metadata to see actual field names ---
test(
    "CAL FIRE service metadata (layer info — shows real field names)",
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
    "/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0?f=json"
)

# --- calfire: try with outFields=* and no where clause at all ---
test(
    "CAL FIRE perimeters — outFields=* no filter",
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
    "/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=2"
)

# --- calfire: maybe it's layer 1 or 2 not 0 ---
test(
    "CAL FIRE perimeters — layer index 1",
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
    "/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/1/query"
    "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=2"
)

# --- arcgis open data geojson download (static but recent) ---
test(
    "CAL FIRE CNRA GeoJSON download endpoint",
    "https://gis.data.cnra.ca.gov/api/download/v1/items"
    "/025fb2ea05f14890b2b11573341b5b18/geojson?layers=0"
)

print("\ndone.")