import csv
import io
import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path
from shapely.geometry import Point, shape

load_dotenv()

# config
INPUT_FILE   = "org_addresses.csv"
OUTPUT_DIR   = Path("outputs")
OUTPUT_ALERTS   = OUTPUT_DIR / "grantee_hazard_alerts.csv"
OUTPUT_FAILURES = OUTPUT_DIR / "geocode_failures.csv"

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

# calfire + nifc + firis combined perimeter layer
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


# geocoding

def geocode_batch(unique_addresses):
    # census batch geocoder response: 8 fields, lon/lat packed as "lon,lat" in field [5]
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
            "https://geocoding.geo.census.gov/geocoder/locations/addressbatch",
            files={"addressFile": ("addresses.csv", payload, "text/csv")},
            data={"benchmark": "Public_AR_Current"},
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
            if match_status == "Match" and len(parts) > 5:
                lonlat = parts[5].strip().split(",")
                if len(lonlat) == 2:
                    try:
                        lon = float(lonlat[0])
                        lat = float(lonlat[1])
                    except ValueError:
                        pass
            addr_key = id_to_key.get(gid)
            if addr_key is None:
                continue
            results[addr_key] = {"geocode_match": match_status, "lat": lat, "lon": lon}

        print(f"  batch {start}–{start + len(chunk)} done")

    geo_df = unique_addresses.copy()
    geo_df["geocode_match"] = geo_df["_addr_key"].map(lambda k: results.get(k, {}).get("geocode_match", "No_Match"))
    geo_df["lat"] = geo_df["_addr_key"].map(lambda k: results.get(k, {}).get("lat"))
    geo_df["lon"] = geo_df["_addr_key"].map(lambda k: results.get(k, {}).get("lon"))
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


# main

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

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

    unique_addrs = (
        grants[[COL_ORG_NAME, "_street_clean", COL_CITY, COL_STATE, COL_ZIP, "_addr_key"]]
        .drop_duplicates(subset=["_addr_key"])
        .reset_index(drop=True)
    )
    print(f"  {len(unique_addrs)} unique addresses after deduplication\n")

    geocoded = geocode_batch(unique_addrs)
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

    geo_lookup = geocoded.set_index("_addr_key")[["lat", "lon", "geocode_match"]].to_dict("index")
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
        base["check_status"]  = status

        if alerts:
            for alert in alerts:
                output_rows.append({**base, **alert})
        else:
            output_rows.append({**base, **empty_alert})

    out_df = pd.DataFrame(output_rows)
    out_df.to_csv(OUTPUT_ALERTS, index=False)

    total   = out_df["Request: Reference Number"].nunique()
    alerted = out_df[out_df["check_status"].str.contains("alert", na=False)]["Request: Reference Number"].nunique()
    errored = out_df[out_df["check_status"].str.contains("error|skipped", na=False)]["Request: Reference Number"].nunique()
    clean   = out_df[out_df["check_status"] == "clean"]["Request: Reference Number"].nunique()
    failed  = out_df[out_df["check_status"] == "geocode_failed"]["Request: Reference Number"].nunique()

    print(f"\n--- run summary ---")
    print(f"  total grants:      {total}")
    print(f"  clean:             {clean}")
    print(f"  alerts found:      {alerted}")
    print(f"  check errors:      {errored}")
    print(f"  geocode failed:    {failed}")
    print(f"  results saved to {OUTPUT_ALERTS}")

    if alerted > 0:
        print("\n--- alerts by source ---")
        alert_df = out_df[out_df["alert_event"] != ""]
        summary = (
            alert_df.groupby([COL_ORG_NAME, COL_STATE, "source", "alert_event", "alert_severity"])
            .size().reset_index(name="count")
            .sort_values(["alert_severity", "source", COL_STATE])
        )
        print(summary.to_string(index=False))

    if errored > 0:
        print("\n--- check errors (results may be incomplete) ---")
        error_df = out_df[out_df["check_status"].str.contains("error|skipped", na=False)]
        for org in error_df[COL_ORG_NAME].unique():
            r2 = error_df[error_df[COL_ORG_NAME] == org].iloc[0]
            print(f"  {org}: {r2['check_status']}")


if __name__ == "__main__":
    main()