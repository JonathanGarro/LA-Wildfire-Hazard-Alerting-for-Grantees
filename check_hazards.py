"""
check_hazard_alerts.py

checks active hazard alerts for a list of grantee addresses against:
  1. nws active alerts (no key required)
  2. calfire + nifc + firis fire perimeters (no key required, ca only)
  3. cal oes evacuation zones (no key required, ca only)
  4. epa airnow aqi (optional, set AIRNOW_API_KEY in .env)
  5. nasa firms hotspot detections (optional, set FIRMS_MAP_KEY in .env)

install deps: pip install requests pandas shapely python-dotenv

input: org_addresses.csv (salesforce/gms export)
output: outputs/grantee_hazard_alerts.csv, outputs/geocode_failures.csv
"""

import csv
import io
import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime
from pathlib import Path
from shapely.geometry import Point, shape

load_dotenv()

# config
INPUT_FILE   = "org_addresses.csv"
OUTPUT_DIR   = Path("outputs")
GEOCODE_CACHE   = OUTPUT_DIR / "geocode_cache.json"  # static, persists across runs

def _stamped(name):
    return OUTPUT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"

# api keys — set in .env, leave blank to skip that source
AIRNOW_API_KEY = os.getenv("AIRNOW_API_KEY", "")
FIRMS_MAP_KEY  = os.getenv("FIRMS_MAP_KEY", "")

# nws
NWS_USER_AGENT      = os.getenv("NWS_USER_AGENT", "HewlettFoundationHazardCheck, data@hewlett.org")
NWS_RATE_LIMIT      = 0.5
NWS_SEVERITY_FILTER = ["Extreme", "Severe", "Moderate", "Minor"]

# airnow: flag aqi >= this (101 = unhealthy for sensitive groups)
AIRNOW_AQI_THRESHOLD = int(os.getenv("AIRNOW_AQI_THRESHOLD", "101"))

# firms
FIRMS_RADIUS_KM      = int(os.getenv("FIRMS_RADIUS_KM", "10"))
FIRMS_DAYS           = int(os.getenv("FIRMS_DAYS", "1"))
FIRMS_MIN_CONFIDENCE = os.getenv("FIRMS_MIN_CONFIDENCE", "nominal")

# calfire + nifc + firis combined perimeter layer (what calfire's incident map uses)
# confirmed fields: incident_name, area_acres, source, displayStatus, FireDiscoveryDate, EditDate
NIFC_PERIMETERS_URL = (
    "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services"
    "/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0/query"
    "?where=displayStatus+%3C%3E+%27Inactive%27"
    "&outFields=incident_name,area_acres,source,type,displayStatus,"
    "poly_DateCurrent,FireDiscoveryDate,EditDate"
    "&f=geojson&resultRecordCount=2000"
)

# cal oes evacuation layer (ca only, refreshed every 10 min)
# confirmed fields: STATUS, COUNTY, ZONE_NAME, ZONE_ID, NOTES
CAL_OES_EVAC_URL = (
    "https://services.arcgis.com/BLN4oKB0N1YSgvY8/arcgis/rest/services"
    "/CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0/query"
    "?where=STATUS+NOT+IN+(%27Normal%27%2C%27NORMAL%27)"
    "&outFields=STATUS,COUNTY,ZONE_NAME,ZONE_ID,NOTES&f=geojson&resultRecordCount=2000"
)

# retry
RETRY_ATTEMPTS = 3
RETRY_BACKOFF  = 2.0
RETRY_ON_CODES = {429, 500, 502, 503, 504}

# salesforce column names
COL_ORG_NAME = "Organization: Organization Name"
COL_STREET   = "Organization: Primary Address Street"
COL_CITY     = "Organization: Primary Address City"
COL_STATE    = "Organization: Primary Address State/Province"
COL_ZIP      = "Organization: Primary Address Zip/Postal Code"
COL_COUNTRY  = "Organization: Primary Address Country"
COL_EIN      = "Organization: EIN"


# retry wrappers

def get_with_retry(url, headers=None, timeout=15, label="request"):
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r, None
            if r.status_code in RETRY_ON_CODES and attempt < RETRY_ATTEMPTS:
                print(f"    {label}: http {r.status_code}, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2; continue
            return None, f"http {r.status_code}"
        except requests.Timeout:
            if attempt < RETRY_ATTEMPTS:
                print(f"    {label}: timeout, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2
            else:
                return None, "timeout after all retries"
        except requests.ConnectionError as e:
            if attempt < RETRY_ATTEMPTS:
                print(f"    {label}: connection error, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2
            else:
                return None, f"connection error: {e}"
        except requests.RequestException as e:
            return None, f"request error: {e}"
    return None, "all retries exhausted"


def post_with_retry(url, files=None, data=None, timeout=120, label="request"):
    delay = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.post(url, files=files, data=data, timeout=timeout)
            if r.status_code == 200:
                return r, None
            if r.status_code in RETRY_ON_CODES and attempt < RETRY_ATTEMPTS:
                print(f"    {label}: http {r.status_code}, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2; continue
            return None, f"http {r.status_code}"
        except requests.Timeout:
            if attempt < RETRY_ATTEMPTS:
                print(f"    {label}: timeout, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2
            else:
                return None, "timeout after all retries"
        except requests.ConnectionError as e:
            if attempt < RETRY_ATTEMPTS:
                print(f"    {label}: connection error, retrying in {delay}s (attempt {attempt}/{RETRY_ATTEMPTS})...")
                time.sleep(delay); delay *= 2
            else:
                return None, f"connection error: {e}"
        except requests.RequestException as e:
            return None, f"request error: {e}"
    return None, "all retries exhausted"


# address normalization

def normalize_street(street):
    if pd.isna(street):
        return ""
    return " ".join(line.strip() for line in str(street).splitlines() if line.strip())


def build_address_key(row):
    return f"{row['_street_clean']}|{row[COL_CITY]}|{row[COL_STATE]}|{row[COL_ZIP]}"


# geocode cache

def load_geocode_cache():
    if GEOCODE_CACHE.exists():
        import json
        with open(GEOCODE_CACHE) as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache):
    import json
    with open(GEOCODE_CACHE, "w") as f:
        json.dump(cache, f, indent=2)


def make_cache_key(ein, addr_key):
    # keyed on EIN + full address string so address changes trigger re-geocode
    return f"{ein}|{addr_key}"


# geocoding

def geocode_batch(unique_addresses):
    # uses the geographies endpoint to get lat/lon + county/state FIPS in one call.
    #
    # response fields (geographies endpoint, csv input, no header):
    #   [0]  id
    #   [1]  input address (echoed)
    #   [2]  match indicator  (Match / No_Match / Tie)
    #   [3]  match type       (Exact / Non_Exact)
    #   [4]  output address
    #   [5]  "lon,lat"        (split on comma)
    #   [6]  tiger line id
    #   [7]  side
    #   [8]  state fips       (2-digit)
    #   [9]  county fips      (3-digit, combine with [8] for full 5-digit fips)
    #   [10] census tract
    #   [11] census block
    print(f"geocoding {len(unique_addresses)} unique addresses...")
    results = {}
    chunk_size = 1000

    for start in range(0, len(unique_addresses), chunk_size):
        chunk = unique_addresses.iloc[start : start + chunk_size]
        csv_lines = []
        id_to_key = {}
        for i, (_, row) in enumerate(chunk.iterrows()):
            gid = start + i
            id_to_key[gid] = row["_addr_key"]
            csv_lines.append(
                f'{gid},"{row["_street_clean"]}","{row[COL_CITY]}",'
                f'"{row[COL_STATE]}","{row[COL_ZIP]}"'
            )
        payload = "\n".join(csv_lines)
        response, err = post_with_retry(
            "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch",
            files={"addressFile": ("addresses.csv", payload, "text/csv")},
            data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
            timeout=120,
            label="census geocoder",
        )
        if err:
            print(f"  geocoder failed for batch {start}–{start+len(chunk)}: {err}")
            continue

        reader = csv.reader(io.StringIO(response.text))
        for parts in reader:
            if len(parts) < 3:
                continue
            try:
                gid = int(parts[0].strip())
            except ValueError:
                continue
            match_status = parts[2].strip()
            lon, lat = None, None
            state_fips = county_fips = county_fips_full = tract = ""
            if match_status == "Match":
                if len(parts) > 5:
                    lonlat = parts[5].strip().split(",")
                    if len(lonlat) == 2:
                        try:
                            lon = float(lonlat[0])
                            lat = float(lonlat[1])
                        except ValueError:
                            pass
                state_fips  = parts[8].strip().zfill(2)  if len(parts) > 8  else ""
                county_fips = parts[9].strip().zfill(3)  if len(parts) > 9  else ""
                tract       = parts[10].strip().zfill(6) if len(parts) > 10 else ""
                county_fips_full = state_fips + county_fips if state_fips and county_fips else ""

            addr_key = id_to_key.get(gid)
            if addr_key is None:
                continue
            results[addr_key] = {
                "geocode_match":    match_status,
                "lat":              lat,
                "lon":              lon,
                "state_fips":       state_fips,
                "county_fips":      county_fips_full,
                "census_tract":     tract,
            }

        print(f"  batch {start}–{start + len(chunk)} done")

    geo_df = unique_addresses.copy()
    for col, default in [
        ("geocode_match", "No_Match"), ("lat", None), ("lon", None),
        ("state_fips", ""), ("county_fips", ""), ("census_tract", ""),
    ]:
        geo_df[col] = geo_df["_addr_key"].map(lambda k, c=col, d=default: results.get(k, {}).get(c, d))

    matched = geo_df["geocode_match"].eq("Match").sum()
    print(f"  {matched}/{len(geo_df)} addresses geocoded successfully\n")
    return geo_df


# source 1: nws

def get_nws_alerts(lat, lon):
    url = f"https://api.weather.gov/alerts/active?point={round(lat,4)},{round(lon,4)}"
    headers = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}
    r, err = get_with_retry(url, headers=headers, timeout=15, label="nws")
    if err:
        return None, err
    alerts = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        if p.get("severity") not in NWS_SEVERITY_FILTER:
            continue
        alerts.append({
            "source": "NWS", "alert_event": p.get("event", ""),
            "alert_severity": p.get("severity", ""), "alert_urgency": p.get("urgency", ""),
            "alert_certainty": p.get("certainty", ""), "alert_headline": p.get("headline", ""),
            "alert_description": (p.get("description") or "")[:500],
            "alert_onset": p.get("onset", ""), "alert_expires": p.get("expires", ""),
            "alert_area_desc": p.get("areaDesc", ""), "alert_sender": p.get("senderName", ""),
            "fire_name": "", "fire_acres": "", "fire_contained_pct": "",
            "fire_discovered": "", "fire_updated": "",
            "evac_zone": "", "evac_status": "", "evac_county": "",
            "aqi_value": "", "aqi_category": "", "aqi_pollutant": "",
            "firms_instrument": "", "firms_confidence": "", "firms_frp": "",
        })
    return alerts, None


# source 2: calfire/nifc/firis perimeters

def fetch_nifc_perimeters():
    print("fetching active fire perimeters...")
    r, err = get_with_retry(NIFC_PERIMETERS_URL, timeout=30, label="nifc")
    if err:
        print(f"  failed: {err} -- fire perimeter check will be skipped")
        return None, err
    features = r.json().get("features", [])
    print(f"  {len(features)} active perimeter(s) loaded (calfire + nifc + firis)")
    perimeters = []
    for f in features:
        geom = f.get("geometry")
        if geom:
            try:
                perimeters.append((shape(geom), f.get("properties", {})))
            except Exception:
                pass
    return perimeters, None


def check_nifc(lat, lon, perimeters):
    pt = Point(lon, lat)
    hits = []
    for polygon, props in perimeters:
        if polygon.contains(pt):
            name = props.get("incident_name") or props.get("mission", "Unknown")
            hits.append({
                "source": "NIFC", "alert_event": "Active Wildfire",
                "alert_severity": "Extreme", "alert_urgency": "Immediate",
                "alert_certainty": "Observed",
                "alert_headline": f"Inside active fire perimeter: {name}",
                "alert_description": f"{round(props.get('area_acres', 0))} acres ({props.get('source','?')})",
                "alert_onset": str(props.get("FireDiscoveryDate") or ""),
                "alert_expires": "", "alert_area_desc": name, "alert_sender": "NIFC WFIGS",
                "fire_name": name, "fire_acres": props.get("area_acres", ""),
                "fire_contained_pct": "",
                "fire_discovered": str(props.get("FireDiscoveryDate") or ""),
                "fire_updated": str(props.get("EditDate") or ""),
                "evac_zone": "", "evac_status": "", "evac_county": "",
                "aqi_value": "", "aqi_category": "", "aqi_pollutant": "",
                "firms_instrument": "", "firms_confidence": "", "firms_frp": "",
            })
    return hits


# source 3: cal oes evacuation zones

def fetch_caloes_evacuations():
    print("fetching cal oes evacuation zones...")
    r, err = get_with_retry(CAL_OES_EVAC_URL, timeout=30, label="cal oes")
    if err:
        print(f"  failed: {err} -- evacuation zone check will be skipped")
        return None, err
    features = r.json().get("features", [])
    print(f"  {len(features)} active evacuation zone(s) loaded")
    zones = []
    for f in features:
        geom = f.get("geometry")
        if geom:
            try:
                zones.append((shape(geom), f.get("properties", {})))
            except Exception:
                pass
    return zones, None


def check_caloes(lat, lon, zones):
    pt = Point(lon, lat)
    hits = []
    for polygon, props in zones:
        if polygon.contains(pt):
            status = props.get("STATUS", "UNKNOWN").upper()
            severity = "Extreme" if status == "EVACUATION ORDER" else "Severe"
            zone_label = props.get("ZONE_NAME") or props.get("ZONE_ID", "Unknown Zone")
            hits.append({
                "source": "CalOES", "alert_event": status.title(),
                "alert_severity": severity,
                "alert_urgency": "Immediate" if status == "EVACUATION ORDER" else "Expected",
                "alert_certainty": "Observed",
                "alert_headline": f"{status.title()}: {zone_label}",
                "alert_description": props.get("NOTES", ""),
                "alert_onset": "", "alert_expires": "",
                "alert_area_desc": props.get("COUNTY", ""),
                "alert_sender": "California OES",
                "fire_name": "", "fire_acres": "", "fire_contained_pct": "",
                "fire_discovered": "", "fire_updated": "",
                "evac_zone": zone_label, "evac_status": props.get("STATUS", ""),
                "evac_county": props.get("COUNTY", ""),
                "aqi_value": "", "aqi_category": "", "aqi_pollutant": "",
                "firms_instrument": "", "firms_confidence": "", "firms_frp": "",
            })
    return hits


# source 4: airnow aqi

def get_airnow_aqi(lat, lon, api_key):
    url = (
        f"https://www.airnowapi.org/aq/observation/latLong/current/"
        f"?format=application/json"
        f"&latitude={round(lat,4)}&longitude={round(lon,4)}"
        f"&distance=25&API_KEY={api_key}"
    )
    r, err = get_with_retry(url, timeout=15, label="airnow")
    if err:
        return None, err
    observations = r.json()
    if not observations:
        return [], None
    worst = max(observations, key=lambda x: x.get("AQI", 0))
    aqi_val  = worst.get("AQI", 0)
    category = worst.get("Category", {}).get("Name", "")
    pollutant = worst.get("ParameterName", "")
    if aqi_val < AIRNOW_AQI_THRESHOLD:
        return [], None
    severity_map = {
        "Good": "Minor", "Moderate": "Minor",
        "Unhealthy for Sensitive Groups": "Moderate",
        "Unhealthy": "Severe", "Very Unhealthy": "Extreme", "Hazardous": "Extreme",
    }
    return [{
        "source": "AirNow", "alert_event": f"Air Quality: {category}",
        "alert_severity": severity_map.get(category, "Moderate"),
        "alert_urgency": "Expected", "alert_certainty": "Observed",
        "alert_headline": f"AQI {aqi_val} ({category}) — {pollutant}",
        "alert_description": f"Current AQI: {aqi_val}. Primary pollutant: {pollutant}.",
        "alert_onset": "", "alert_expires": "",
        "alert_area_desc": worst.get("ReportingArea", ""), "alert_sender": "EPA AirNow",
        "fire_name": "", "fire_acres": "", "fire_contained_pct": "",
        "fire_discovered": "", "fire_updated": "",
        "evac_zone": "", "evac_status": "", "evac_county": "",
        "aqi_value": aqi_val, "aqi_category": category, "aqi_pollutant": pollutant,
        "firms_instrument": "", "firms_confidence": "", "firms_frp": "",
    }], None


# source 5: nasa firms

def get_firms_hotspots(lat, lon, map_key):
    deg_offset = FIRMS_RADIUS_KM / 111.0
    bbox = (
        f"{round(lon - deg_offset, 4)},{round(lat - deg_offset, 4)},"
        f"{round(lon + deg_offset, 4)},{round(lat + deg_offset, 4)}"
    )
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{map_key}/VIIRS_SNPP_NRT/{bbox}/{FIRMS_DAYS}"
    r, err = get_with_retry(url, timeout=20, label="firms")
    if err:
        return None, err
    text = r.text.strip()
    if not text or text.startswith("Invalid"):
        return [], None
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        return [], None
    if df.empty:
        return [], None
    conf_order = {"low": 0, "nominal": 1, "high": 2}
    min_conf = conf_order.get(FIRMS_MIN_CONFIDENCE.lower(), 1)
    if "confidence" in df.columns:
        df = df[df["confidence"].str.lower().map(conf_order).fillna(0) >= min_conf]
    if df.empty:
        return [], None
    alerts = []
    for _, row in df.iterrows():
        alerts.append({
            "source": "FIRMS", "alert_event": "Satellite Fire Detection",
            "alert_severity": "Severe", "alert_urgency": "Immediate",
            "alert_certainty": "Observed",
            "alert_headline": f"Satellite fire hotspot within {FIRMS_RADIUS_KM}km",
            "alert_description": (
                f"VIIRS detection at ({row.get('latitude','?')}, {row.get('longitude','?')}), "
                f"confidence: {row.get('confidence','?')}, FRP: {row.get('frp','?')} MW, "
                f"acquired: {row.get('acq_date','?')} {row.get('acq_time','?')}"
            ),
            "alert_onset": str(row.get("acq_date", "")), "alert_expires": "",
            "alert_area_desc": "", "alert_sender": "NASA FIRMS / VIIRS SNPP",
            "fire_name": "", "fire_acres": "", "fire_contained_pct": "",
            "fire_discovered": "", "fire_updated": "",
            "evac_zone": "", "evac_status": "", "evac_county": "",
            "aqi_value": "", "aqi_category": "", "aqi_pollutant": "",
            "firms_instrument": "VIIRS_SNPP",
            "firms_confidence": str(row.get("confidence", "")),
            "firms_frp": str(row.get("frp", "")),
        })
    return alerts, None



# map generation

def generate_map(out_df, caloes_zones, nifc_perimeters):
    import folium

    OUTPUT_MAP = _stamped("grantee_hazard_map.html")

    # source colors
    SOURCE_COLORS = {
        "CalOES": "#D37072",  # red
        "NIFC":   "#FF6B00",  # orange
        "NWS":    "#4A90D9",  # blue
        "AirNow": "#8B5CF6",  # purple
        "FIRMS":  "#F59E0B",  # amber
    }

    # severity to marker color
    SEVERITY_MARKER = {
        "Extreme": "red",
        "Severe":  "orange",
        "Moderate":"blue",
        "Minor":   "gray",
    }

    # center on alerted grantees, fall back to CA
    all_located = out_df.dropna(subset=["lat", "lon"])
    if not all_located.empty:
        min_lat, max_lat = all_located["lat"].min(), all_located["lat"].max()
        min_lon, max_lon = all_located["lon"].min(), all_located["lon"].max()
        center = [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2]
    else:
        center = [36.7783, -119.4179]
        min_lat, max_lat, min_lon, max_lon = 32.5, 42.0, -124.5, -114.1

    m = folium.Map(location=center, zoom_start=10, tiles="CartoDB positron")
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]], padding=[30, 30])

    # layer group for cal oes evacuation polygons
    if caloes_zones:
        evac_layer = folium.FeatureGroup(name="Cal OES Evacuation Zones", show=True)
        for polygon, props in caloes_zones:
            status = props.get("STATUS", "").upper()
            fill   = "#D37072" if status == "EVACUATION ORDER" else "#E5C447"
            label  = props.get("ZONE_NAME") or props.get("ZONE_ID", "")
            county = props.get("COUNTY", "")
            notes  = props.get("NOTES", "")
            try:
                geojson = polygon.__geo_interface__
                folium.GeoJson(
                    geojson,
                    style_function=lambda f, fill=fill: {
                        "fillColor": fill, "color": fill,
                        "weight": 1.5, "fillOpacity": 0.35,
                    },
                    tooltip=f"{status.title()}<br>{label}<br>{county}{(' — ' + notes) if notes else ''}",
                ).add_to(evac_layer)
            except Exception:
                pass
        evac_layer.add_to(m)

    # layer group for fire perimeters
    if nifc_perimeters:
        fire_layer = folium.FeatureGroup(name="Active Fire Perimeters", show=True)
        for polygon, props in nifc_perimeters:
            name  = props.get("incident_name") or props.get("mission", "Unknown")
            acres = props.get("area_acres", "?")
            src   = props.get("source", "")
            try:
                geojson = polygon.__geo_interface__
                folium.GeoJson(
                    geojson,
                    style_function=lambda f: {
                        "fillColor": "#FF6B00", "color": "#CC4400",
                        "weight": 1.5, "fillOpacity": 0.3,
                    },
                    tooltip=f"Fire: {name}<br>{round(float(acres)) if acres != '?' else '?'} acres ({src})",
                ).add_to(fire_layer)
            except Exception:
                pass
        fire_layer.add_to(m)

    # grantee markers — one per unique address, colored by worst alert
    grantee_layer = folium.FeatureGroup(name="Grantees", show=True)
    seen_coords = set()
    for _, row in out_df.iterrows():
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (ValueError, TypeError):
            continue
        if lat != lat or lon != lon:  # NaN check
            continue
        coord_key = (round(lat, 4), round(lon, 4))
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        org = row[COL_ORG_NAME]
        status = row.get("check_status", "clean")
        severity = row.get("alert_severity", "")
        marker_color = SEVERITY_MARKER.get(severity, "green") if "alert" in status else "green"

        # collect all alerts for this address for the popup
        org_alerts = out_df[
            (out_df["lat"].astype(str) == str(row["lat"])) &
            (out_df["alert_event"] != "")
        ][["source", "alert_event", "alert_headline"]].drop_duplicates()

        if org_alerts.empty:
            popup_html = f"<b>{org}</b><br>No active alerts"
        else:
            rows_html = "".join(
                f"<tr><td style='padding:2px 6px'><span style='color:{SOURCE_COLORS.get(r.source,'#333')}'>"
                f"&#9679;</span> {r.source}</td>"
                f"<td style='padding:2px 6px'>{r.alert_headline}</td></tr>"
                for r in org_alerts.itertuples()
            )
            popup_html = (
                f"<b>{org}</b><br>"
                f"<table style='font-size:12px;margin-top:4px'>{rows_html}</table>"
            )

        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup(popup_html, max_width=350),
            tooltip=org,
            icon=folium.Icon(color=marker_color, icon="building", prefix="fa"),
        ).add_to(grantee_layer)

    grantee_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    m.save(str(OUTPUT_MAP))
    print(f"  map saved to {OUTPUT_MAP}")
    return OUTPUT_MAP

# main

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_ALERTS   = _stamped("grantee_hazard_alerts.csv")
    OUTPUT_FAILURES = _stamped("geocode_failures.csv")


    print("active sources:")
    print("  [x] nws alerts")
    print("  [x] calfire + nifc + firis perimeters")
    print("  [x] cal oes evacuations (ca only)")
    print(f"  {'[x]' if AIRNOW_API_KEY else '[ ]'} airnow aqi {'(key set)' if AIRNOW_API_KEY else '(no key -- skipping)'}")
    print(f"  {'[x]' if FIRMS_MAP_KEY else '[ ]'} nasa firms {'(key set)' if FIRMS_MAP_KEY else '(no key -- skipping)'}")
    print()

    print(f"loading {INPUT_FILE}...")
    grants = pd.read_csv(INPUT_FILE, dtype=str, encoding="latin-1").fillna("")
    print(f"  {len(grants)} grant rows loaded")

    grants["_street_clean"] = grants[COL_STREET].apply(normalize_street)
    grants["_addr_key"] = grants.apply(build_address_key, axis=1)

    ein_col = COL_EIN if COL_EIN in grants.columns else None
    unique_cols = [COL_ORG_NAME, "_street_clean", COL_CITY, COL_STATE, COL_ZIP, "_addr_key"]
    if ein_col:
        unique_cols.insert(1, ein_col)
    unique_addrs = (
        grants[unique_cols]
        .drop_duplicates(subset=["_addr_key"])
        .reset_index(drop=True)
    )
    print(f"  {len(unique_addrs)} unique addresses after deduplication\n")

    cache = load_geocode_cache()
    cache_hits, to_geocode_idx = [], []

    for idx, row in unique_addrs.iterrows():
        ein = row.get(ein_col, "") if ein_col else ""
        ckey = make_cache_key(ein, row["_addr_key"])
        if ckey in cache:
            cache_hits.append({**row.to_dict(), **cache[ckey], "geocode_match": "Match (cached)"})
        else:
            to_geocode_idx.append(idx)

    to_geocode = unique_addrs.loc[to_geocode_idx].reset_index(drop=True)
    print(f"  {len(cache_hits)} address(es) resolved from cache")
    print(f"  {len(to_geocode)} address(es) to geocode\n")

    fresh = geocode_batch(to_geocode) if not to_geocode.empty else pd.DataFrame()

    # update cache with newly geocoded matches
    if not fresh.empty:
        for _, row in fresh[fresh["geocode_match"] == "Match"].iterrows():
            ein = row.get(ein_col, "") if ein_col else ""
            ckey = make_cache_key(ein, row["_addr_key"])
            cache[ckey] = {
                "lat": row["lat"], "lon": row["lon"],
                "state_fips": row["state_fips"],
                "county_fips": row["county_fips"],
                "census_tract": row["census_tract"],
            }
        save_geocode_cache(cache)
        print(f"  geocode cache updated ({len(cache)} total entries)")

    # merge cache hits and fresh results
    cache_df = pd.DataFrame(cache_hits) if cache_hits else pd.DataFrame()
    geocoded = pd.concat([cache_df, fresh], ignore_index=True) if not fresh.empty else cache_df

    matched_addrs = geocoded[geocoded["lat"].notna()].copy()
    failed_addrs  = geocoded[geocoded["lat"].isna()].copy()

    if not failed_addrs.empty:
        print(f"warning: {len(failed_addrs)} address(es) could not be geocoded:")
        for _, row in failed_addrs.iterrows():
            print(f"  {row[COL_ORG_NAME]} — {row['_street_clean']}, {row[COL_CITY]}, {row[COL_STATE]}")
        failed_addrs.drop(columns=["_street_clean", "_addr_key"]).to_csv(OUTPUT_FAILURES, index=False)
        print(f"  failures saved to {OUTPUT_FAILURES}\n")

    nifc_perimeters, nifc_err   = fetch_nifc_perimeters()
    caloes_zones,    caloes_err = fetch_caloes_evacuations()
    print()

    print(f"checking {len(matched_addrs)} locations...")
    addr_alerts   = {}
    addr_statuses = {}

    for i, (_, row) in enumerate(matched_addrs.iterrows()):
        label = f"{row[COL_ORG_NAME]} ({row[COL_CITY]}, {row[COL_STATE]})"
        print(f"  [{i+1}/{len(matched_addrs)}] {label}...")

        all_alerts  = []
        error_flags = []

        # nws
        nws_alerts, nws_err = get_nws_alerts(row["lat"], row["lon"])
        if nws_err:
            print(f"    nws failed: {nws_err}")
            error_flags.append("nws_error")
        else:
            all_alerts.extend(nws_alerts)

        # nifc (local, no api call)
        if nifc_perimeters is not None:
            all_alerts.extend(check_nifc(row["lat"], row["lon"], nifc_perimeters))
        else:
            error_flags.append("nifc_skipped")

        # cal oes (local, ca only)
        if row[COL_STATE].strip().upper() in ("CA", "CALIFORNIA"):
            if caloes_zones is not None:
                all_alerts.extend(check_caloes(row["lat"], row["lon"], caloes_zones))
            else:
                error_flags.append("caloes_skipped")

        # airnow
        if AIRNOW_API_KEY:
            aqi_alerts, aqi_err = get_airnow_aqi(row["lat"], row["lon"], AIRNOW_API_KEY)
            if aqi_err:
                print(f"    airnow failed: {aqi_err}")
                error_flags.append("airnow_error")
            else:
                all_alerts.extend(aqi_alerts)

        # firms
        if FIRMS_MAP_KEY:
            firms_alerts, firms_err = get_firms_hotspots(row["lat"], row["lon"], FIRMS_MAP_KEY)
            if firms_err:
                print(f"    firms failed: {firms_err}")
                error_flags.append("firms_error")
            else:
                all_alerts.extend(firms_alerts)

        if error_flags:
            status = "+".join(error_flags) + ("+alert" if all_alerts else "")
        elif all_alerts:
            status = "alert"
        else:
            status = "clean"

        if all_alerts:
            sources = ", ".join(sorted({a["source"] for a in all_alerts}))
            print(f"    {len(all_alerts)} alert(s) [{sources}] -- status: {status}")
        else:
            print(f"    status: {status}")

        addr_alerts[row["_addr_key"]]   = all_alerts
        addr_statuses[row["_addr_key"]] = status
        time.sleep(NWS_RATE_LIMIT)

    geo_lookup = geocoded.set_index("_addr_key")[["lat", "lon", "geocode_match", "state_fips", "county_fips", "census_tract"]].to_dict("index")
    empty_alert = {
        "source": "", "alert_event": "", "alert_severity": "", "alert_urgency": "",
        "alert_certainty": "", "alert_headline": "", "alert_description": "",
        "alert_onset": "", "alert_expires": "", "alert_area_desc": "", "alert_sender": "",
        "fire_name": "", "fire_acres": "", "fire_contained_pct": "",
        "fire_discovered": "", "fire_updated": "",
        "evac_zone": "", "evac_status": "", "evac_county": "",
        "aqi_value": "", "aqi_category": "", "aqi_pollutant": "",
        "firms_instrument": "", "firms_confidence": "", "firms_frp": "",
    }

    output_rows = []
    for _, grant in grants.iterrows():
        key    = grant["_addr_key"]
        geo    = geo_lookup.get(key, {})
        alerts = addr_alerts.get(key, [])
        status = addr_statuses.get(key, "geocode_failed")

        base = grant.drop(labels=["_street_clean", "_addr_key"]).to_dict()
        base["lat"]           = geo.get("lat")
        base["lon"]           = geo.get("lon")
        base["geocode_match"] = geo.get("geocode_match", "not_attempted")
        base["state_fips"]    = geo.get("state_fips", "")
        base["county_fips"]   = geo.get("county_fips", "")
        base["census_tract"]  = geo.get("census_tract", "")
        base["check_status"]  = status

        if alerts:
            for alert in alerts:
                output_rows.append({**base, **alert})
        else:
            output_rows.append({**base, **empty_alert})

    out_df = pd.DataFrame(output_rows)

    # add has_alert flag (true if any alert field is populated)
    alert_cols = ["source", "alert_event", "alert_severity", "alert_urgency",
                  "alert_certainty", "alert_headline", "alert_description",
                  "evac_zone", "evac_status", "fire_name", "aqi_value", "firms_frp"]
    out_df["has_alert"] = out_df[[c for c in alert_cols if c in out_df.columns]].apply(
        lambda row: any(str(v).strip() not in ("", "nan") for v in row), axis=1
    )

    # collapse to one row per org+alert combo.
    # grant-specific columns (ref number, project title, request id) are
    # aggregated as comma-separated values; all other columns are org- or
    # alert-level and used as the group key.
    grant_cols = {
        "Request: Reference Number": "Grant Reference Numbers",
        "Project Title":             "Project Titles",
        "Request: ID":               "Request IDs",
    }
    # only collapse columns that actually exist in the output
    grant_cols = {k: v for k, v in grant_cols.items() if k in out_df.columns}
    group_cols = [c for c in out_df.columns if c not in grant_cols and c != "has_alert"]

    agg = {k: lambda s, k=k: ", ".join(sorted(s.dropna().astype(str).unique()))
           for k in grant_cols}
    collapsed = (
        out_df.groupby(group_cols, dropna=False)
        .agg(agg)
        .reset_index()
        .rename(columns=grant_cols)
    )

    # re-derive has_alert after groupby (groupby drops it as a non-numeric non-key)
    alert_cols = ["source", "alert_event", "alert_severity", "alert_urgency",
                  "alert_certainty", "alert_headline", "alert_description",
                  "evac_zone", "evac_status", "fire_name", "aqi_value", "firms_frp"]
    collapsed["has_alert"] = collapsed[[c for c in alert_cols if c in collapsed.columns]].apply(
        lambda row: any(str(v).strip() not in ("", "nan") for v in row), axis=1
    )

    # put has_alert and collapsed grant columns at the far left
    front_cols = ["has_alert"] + list(grant_cols.values())
    other_cols = [c for c in collapsed.columns if c not in front_cols]
    collapsed  = collapsed[front_cols + other_cols]

    collapsed.to_csv(OUTPUT_ALERTS, index=False)
    out_df = collapsed  # keep downstream summary code working

    total   = len(out_df)
    alerted = out_df["has_alert"].sum()
    errored = out_df[out_df["check_status"].str.contains("error|skipped", na=False)].shape[0]
    clean   = out_df[out_df["check_status"] == "clean"].shape[0]
    failed  = out_df[out_df["check_status"] == "geocode_failed"].shape[0]

    print(f"\n--- run summary ---")
    print(f"  total grants:      {total}")
    print(f"  clean:             {clean}")
    print(f"  alerts found:      {alerted}")
    print(f"  check errors:      {errored}")
    print(f"  geocode failed:    {failed}")
    print(f"  results saved to {OUTPUT_ALERTS}")

    if alerted > 0:
        print("\n--- alerts by source ---")
        alert_df = out_df[out_df["has_alert"]]
        summary = (
            alert_df.groupby([COL_ORG_NAME, COL_STATE, "source", "alert_event", "alert_severity"])
            .size().reset_index(name="count")
            .sort_values(["alert_severity", "source", COL_STATE])
        )
        print(summary.to_string(index=False))

    if errored > 0:
        print("\n--- check errors (results may be incomplete) ---")
        error_df = out_df[out_df["check_status"].str.contains("error|skipped", na=False)]
        for _, r2 in error_df[[COL_ORG_NAME, "check_status"]].drop_duplicates().iterrows():
            print(f"  {r2[COL_ORG_NAME]}: {r2['check_status']}")

    print("\ngenerating map...")
    generate_map(out_df, caloes_zones, nifc_perimeters)


if __name__ == "__main__":
    main()