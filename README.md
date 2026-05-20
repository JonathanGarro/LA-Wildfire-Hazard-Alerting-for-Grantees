# Grantee Hazard Alerting 

Checks active hazard alerts for a list of grantee addresses. Takes a Salesforce/GMS grant export, geocodes the addresses, queries multiple live hazard data sources, and produces a CSV report and an interactive map.

---

## Setup

### 1. Project folder structure

```
LA Wildfire Hazard Alerting Proof of Concept/
├── check_hazards.py
├── org_addresses.csv
├── .env                  ← create from .env.example
├── .env.example
├── outputs/
│   ├── grantee_hazard_alerts.csv
│   ├── grantee_hazard_map.html
│   └── geocode_failures.csv
└── README.md
```

### 2. Install dependencies

```bash
pip install requests pandas shapely folium python-dotenv
```

### 3. Configure API keys

Copy `.env.example` to `.env` and fill in any keys. The script runs without them but those sources will be skipped.

```bash
cp .env.example .env
```

| Variable | Source | Signup |
|---|---|---|
| `AIRNOW_API_KEY` | EPA AirNow AQI | https://docs.airnowapi.org |
| `FIRMS_MAP_KEY` | NASA FIRMS satellite hotspots | https://firms.modaps.eosdis.nasa.gov/api/map_key |

All other configuration options can also be overridden in `.env` (see Configuration below).

### 4. Prepare your input file

Export active grants from Salesforce. Required columns (standard GMS report format):

| Column | Notes |
|---|---|
| `Request: Reference Number` | Grant ID |
| `Organization: Organization Name` | |
| `Project Title` | |
| `Organization: Primary Address Street` | Multi-line fields (suite on second line) handled automatically |
| `Organization: Primary Address City` | |
| `Organization: Primary Address State/Province` | |
| `Organization: Primary Address Zip/Postal Code` | |
| `Organization: Primary Address Country` | US only; international addresses will fail geocoding |

Any additional columns in the export (e.g. `Organization: EIN`, `Request: ID`, `Organization: ID`) pass through to the output automatically.

Save as `org_addresses.csv` in the project folder. The filename can be changed via `INPUT_FILE` in `.env`.

### 5. Run

```bash
python check_hazards.py
```

---

## Data sources

Five sources are checked on each run. Three require no credentials. Two are optional.

### 1. NWS Active Alerts (no key required)
**National Weather Service** — weather watches, warnings, and advisories including Red Flag Warnings, Flash Flood Warnings, Extreme Heat Warnings, and Tornado Warnings. Queried once per unique geocoded address via the `?point=lat,lon` endpoint, which handles spatial lookup server-side.

API: `https://api.weather.gov/alerts/active?point={lat},{lon}`

### 2. CAL FIRE + NIFC + FIRIS Fire Perimeters (no key required, California only)
**Combined perimeter layer** used by CAL FIRE's own public incident map. Aggregates three sources: CAL FIRE Intel remote sensing, FIRIS aerial infrared (near real-time from fixed-wing aircraft funded by CalOES), and NIFC WFIGS interagency perimeters. Fetched once at startup; point-in-polygon checks run locally with Shapely. Inactive perimeters are filtered out at query time.

Note: this service is California-only. A national NIFC endpoint was evaluated but requires authentication.

API: `services1.arcgis.com/jUJYIo9tSA7EHvfZ/.../CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0`

### 3. Cal OES Evacuation Zones (no key required, California only)
**California Office of Emergency Services** — aggregates evacuation orders and warnings from county sources statewide, refreshed every 10 minutes. Fetched once at startup; checked locally. Only runs for grantees with a CA state code. Distinguishes between **Evacuation Warning** (potential threat, prepare to leave) and **Evacuation Order** (lawful order to leave immediately).

This is the most important source for wildfire scenarios — it catches evacuation zones that may not yet have a corresponding NWS alert.

Note: not every California county participates. Coverage has expanded significantly since 2024 but gaps exist for some rural counties.

API: `services.arcgis.com/BLN4oKB0N1YSgvY8/.../CA_EVACUATIONS_CalOESHosted_view/FeatureServer/0`

### 4. EPA AirNow AQI (free key required)
**Environmental Protection Agency** — current Air Quality Index by lat/lon. One API call per grantee. Flags locations at or above `AIRNOW_AQI_THRESHOLD` (default: 101, "Unhealthy for Sensitive Groups"). Particularly useful for wildfire smoke, which can affect grantees well outside any fire perimeter or evacuation zone.

| AQI | Category |
|---|---|
| 0–50 | Good |
| 51–100 | Moderate |
| 101–150 | Unhealthy for Sensitive Groups ← default threshold |
| 151–200 | Unhealthy |
| 201–300 | Very Unhealthy |
| 301–500 | Hazardous |

API: `https://www.airnowapi.org/aq/observation/latLong/current/`

### 5. NASA FIRMS Hotspots (free key required)
**Fire Information for Resource Management System** — near real-time satellite fire detections from VIIRS (375m resolution), updated multiple times per day. Searches within `FIRMS_RADIUS_KM` (default: 10km) of each grantee. Catches fires that don't yet have official perimeters drawn. Caution: agricultural burns and industrial heat sources also produce detections — treat hits as a prompt to investigate, not a confirmed wildfire.

API: `https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/{bbox}/{days}`

---

## How it works

```
org_addresses.csv
       │
       ▼
  Normalize streets         collapse multi-line suite fields into one line
       │
       ▼
  Deduplicate addresses      check each unique location once, rejoin to all grants at the end
       │
       ▼
  US Census batch geocoder   free, no key, up to 1000 addresses per batch
       │
       ├─ failures → geocode_failures.csv
       │
       ▼
  Fetch bulk sources once:
    • CAL FIRE + NIFC + FIRIS perimeters
    • Cal OES evacuation zones
       │
       ▼
  Per unique geocoded address:
    • NWS alert lookup          (API call)
    • Fire perimeter check      (local point-in-polygon)
    • Evacuation zone check     (local point-in-polygon, CA only)
    • AirNow AQI                (API call, if key set)
    • FIRMS hotspot search      (API call, if key set)
       │
       ▼
  Rejoin results to full grant list
       │
       ▼
  grantee_hazard_alerts.csv + grantee_hazard_map.html
```

Deduplication is important: geocoding and per-grantee API calls run once per unique address, not once per grant. An org with three active grants at the same address costs one geocode call and one NWS call.

---

## Output

### `outputs/grantee_hazard_alerts.csv`

One row per grant per alert. Grants with no alerts also appear (with blank alert fields) so the file is a complete record of every grant checked — `check_status` tells you why a row is blank.

All columns from the input CSV pass through, plus:

| Column | Description |
|---|---|
| `check_status` | Run outcome — see values below |
| `lat` / `lon` | Geocoded coordinates |
| `geocode_match` | `Match`, `No_Match`, or `not_attempted` |
| `source` | Which source flagged this row: `NWS`, `NIFC`, `CalOES`, `AirNow`, `FIRMS` |
| `alert_event` | Event type (e.g. `Red Flag Warning`, `Evacuation Order`, `Air Quality: Unhealthy`) |
| `alert_severity` | `Extreme`, `Severe`, `Moderate`, `Minor` |
| `alert_urgency` / `alert_certainty` | NWS urgency and certainty fields |
| `alert_headline` | Short human-readable description |
| `alert_description` | Longer detail (truncated to 500 chars for NWS) |
| `alert_onset` / `alert_expires` | Alert timing |
| `alert_area_desc` / `alert_sender` | Area and issuing agency |
| `evac_zone` / `evac_status` / `evac_county` | Cal OES zone details |
| `aqi_value` / `aqi_category` / `aqi_pollutant` | AirNow details |
| `fire_name` / `fire_acres` / `fire_discovered` / `fire_updated` | Fire perimeter details |
| `firms_instrument` / `firms_confidence` / `firms_frp` | FIRMS satellite detection details |

#### `check_status` values

| Status | Meaning |
|---|---|
| `clean` | All sources checked, no alerts found |
| `alert` | One or more alerts found |
| `geocode_failed` | Address could not be geocoded; no hazard check attempted |
| `nws_error` | NWS call failed after retries; other sources still ran |
| `nifc_skipped` | Fire perimeter fetch failed at startup |
| `caloes_skipped` | Cal OES evacuation fetch failed at startup |
| `airnow_error` | AirNow call failed after retries |
| `firms_error` | FIRMS call failed after retries |

Statuses combine with `+` — e.g. `nws_error+alert` means NWS failed but another source found an alert.

### `outputs/grantee_hazard_map.html`

Self-contained interactive HTML map — open in any browser. Contains three toggleable layers:

- **Cal OES Evacuation Zones** — red polygons for orders, yellow for warnings
- **Active Fire Perimeters** — orange polygons from the CAL FIRE + NIFC + FIRIS layer
- **Grantees** — markers colored by severity (green = clean, red = extreme, orange = severe, blue = moderate). Click a marker for a popup listing all active alerts for that location.

The map centers on the alerted grantees if any exist, otherwise defaults to the center of California.

### `outputs/geocode_failures.csv`

Addresses the Census geocoder could not match. Most common cause is P.O. Boxes, which geocoders cannot place by design. Also catches addresses with non-standard formatting or that aren't yet in Census data.

---

## Configuration

All parameters can be set in `.env`. None are required — the script runs with sensible defaults.

```bash
# api keys
AIRNOW_API_KEY=
FIRMS_MAP_KEY=

# nws
NWS_USER_AGENT=HewlettFoundationHazardCheck, data@hewlett.org

# airnow
AIRNOW_AQI_THRESHOLD=101      # flag AQI >= this (101 = unhealthy for sensitive groups)

# firms
FIRMS_RADIUS_KM=10             # search radius around each grantee
FIRMS_DAYS=1                   # days back to look (1 = last 24h)
FIRMS_MIN_CONFIDENCE=nominal   # nominal or high
```

Retry behavior and input/output paths are set as constants at the top of `check_hazards.py`:

```python
INPUT_FILE     = "org_addresses.csv"
RETRY_ATTEMPTS = 3      # total attempts per API call
RETRY_BACKOFF  = 2.0    # seconds; doubles each retry (2s → 4s → 8s)
RETRY_ON_CODES = {429, 500, 502, 503, 504}
```