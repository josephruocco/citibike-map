#!/usr/bin/env python3
"""Build Citi Bike ride summaries and interactive maps from a cleaned CSV."""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import re
import ssl
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


GBFS_MANIFEST_URLS = [
    "https://gbfs.lyft.com/gbfs/2.3/bkn/en.json",
    "https://gbfs.lyft.com/gbfs/2.3/bkn/en-US/en.json",
    "https://gbfs.citibikenyc.com/gbfs/en.json",
]

DIRECT_STATION_INFO_URLS = [
    "https://gbfs.lyft.com/gbfs/2.3/bkn/en/station_information.json",
    "https://gbfs.lyft.com/gbfs/2.3/bkn/en-US/station_information.json",
    "https://gbfs.citibikenyc.com/gbfs/en/station_information.json",
]

BASEMAP_TILE_URL = "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png"
BASEMAP_ATTRIBUTION = "&copy; OpenStreetMap contributors &copy; CARTO"


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str
    lat: float
    lon: float


@dataclass
class MatchResult:
    station: Station | None
    method: str
    score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Citi Bike CSV summaries and interactive HTML maps."
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        default="/Users/josephruocco/Downloads/citibike_rides_clean.csv",
        help="Path to the cleaned Citi Bike rides CSV.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory to write HTML and CSV outputs into.",
    )
    parser.add_argument(
        "--max-flow-lines",
        type=int,
        default=250,
        help="Maximum number of route flow lines to draw on the flows map.",
    )
    parser.add_argument(
        "--fuzzy-cutoff",
        type=float,
        default=0.84,
        help="Minimum similarity score for fuzzy station matching.",
    )
    return parser.parse_args()


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "citibike-map-script/1.0"})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.load(response)


def resolve_station_information_url() -> str:
    for manifest_url in GBFS_MANIFEST_URLS:
        try:
            manifest = fetch_json(manifest_url)
        except Exception:
            continue
        data = manifest.get("data", {})
        for feeds in data.values():
            if not isinstance(feeds, dict):
                continue
            feed_entries = feeds.get("feeds", [])
            for feed in feed_entries:
                if feed.get("name") == "station_information" and feed.get("url"):
                    return feed["url"]

    for url in DIRECT_STATION_INFO_URLS:
        try:
            fetch_json(url)
            return url
        except Exception:
            continue

    raise RuntimeError(
        "Unable to fetch Citi Bike station information. "
        "Check network access or supply a working GBFS URL in the script."
    )


def load_stations() -> list[Station]:
    url = resolve_station_information_url()
    payload = fetch_json(url)
    stations_data = payload.get("data", {}).get("stations", [])
    stations: list[Station] = []
    for row in stations_data:
        try:
            stations.append(
                Station(
                    station_id=str(row["station_id"]),
                    name=str(row["name"]).strip(),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    if not stations:
        raise RuntimeError("Station feed loaded, but no stations were found.")
    return stations


def clean_station_name(raw_name: str | None) -> str:
    value = (raw_name or "").strip()
    value = re.sub(r"(Started|Ended) at \d{1,2}:\d{2} [AP]M\s*$", "", value).strip()
    return value


def normalize_station_name(name: str) -> str:
    value = clean_station_name(name).lower()
    value = value.replace("&", " and ")
    substitutions = {
        r"\bsaint\b": "st",
        r"\bstreet\b": "st",
        r"\bavenue\b": "ave",
        r"\bplace\b": "pl",
        r"\bboulevard\b": "blvd",
        r"\broad\b": "rd",
        r"\bwest\b": "w",
        r"\beast\b": "e",
        r"\bnorth\b": "n",
        r"\bsouth\b": "s",
    }
    for pattern, replacement in substitutions.items():
        value = re.sub(pattern, replacement, value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def build_station_indexes(stations: Iterable[Station]) -> tuple[dict[str, list[Station]], dict[str, Station]]:
    by_normalized: dict[str, list[Station]] = defaultdict(list)
    by_display_name: dict[str, Station] = {}
    for station in stations:
        by_normalized[normalize_station_name(station.name)].append(station)
        by_display_name[station.name] = station
    return by_normalized, by_display_name


def match_station(
    raw_name: str,
    normalized_lookup: dict[str, list[Station]],
    normalized_names: list[str],
    fuzzy_cutoff: float,
) -> MatchResult:
    cleaned = clean_station_name(raw_name)
    normalized = normalize_station_name(cleaned)

    if not normalized:
        return MatchResult(station=None, method="blank", score=0.0)

    exact_matches = normalized_lookup.get(normalized)
    if exact_matches:
        return MatchResult(station=exact_matches[0], method="exact", score=1.0)

    candidates = difflib.get_close_matches(normalized, normalized_names, n=1, cutoff=fuzzy_cutoff)
    if not candidates:
        return MatchResult(station=None, method="unmatched", score=0.0)

    best_name = candidates[0]
    best_station = normalized_lookup[best_name][0]
    score = difflib.SequenceMatcher(a=normalized, b=best_name).ratio()
    return MatchResult(station=best_station, method="fuzzy", score=score)


def parse_float(value: str | None) -> float:
    try:
        return float((value or "").strip())
    except ValueError:
        return 0.0


def parse_month(date_text: str | None) -> str:
    if not date_text:
        return "unknown"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_text.strip(), fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return "unknown"


def read_rides(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def map_center_from_points(points: list[tuple[float, float]]) -> tuple[float, float]:
    if not points:
        return 40.73061, -73.935242
    lat = sum(point[0] for point in points) / len(points)
    lon = sum(point[1] for point in points) / len(points)
    return lat, lon


def build_heatmap_html(title: str, subtitle: str, points: list[dict]) -> str:
    center = map_center_from_points([(row["lat"], row["lon"]) for row in points])
    payload = json.dumps(points)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; font-family: Helvetica, Arial, sans-serif; }}
    body {{ background: #f3f4f6; }}
    .panel {{
      position: absolute;
      top: 22px;
      left: 22px;
      z-index: 1000;
      background: rgba(255, 255, 255, 0.96);
      padding: 16px 18px;
      border-radius: 22px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.16);
      max-width: 390px;
      border: 1px solid rgba(0, 0, 0, 0.06);
    }}
    .panel h1 {{ font-size: 17px; margin: 0 0 5px; font-weight: 700; letter-spacing: -0.02em; }}
    .panel p {{ font-size: 13px; margin: 0; line-height: 1.4; color: #5f6368; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <h1>{title}</h1>
    <p>{subtitle}</p>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const points = {payload};
    const map = L.map('map').setView([{center[0]}, {center[1]}], 12);
    L.tileLayer('{BASEMAP_TILE_URL}', {{
      maxZoom: 19,
      subdomains: 'abcd',
      attribution: '{BASEMAP_ATTRIBUTION}'
    }}).addTo(map);

    const bounds = L.latLngBounds(points.map((point) => [point.lat, point.lon]));
    if (points.length) {{
      map.fitBounds(bounds.pad(0.08));
    }}

    points.forEach((point) => {{
      const radius = Math.max(5, Math.min(26, 5 + Math.sqrt(point.count) * 2.8));
      const marker = L.circleMarker([point.lat, point.lon], {{
        radius,
        color: '#1493c7',
        fillColor: '#35b6e8',
        fillOpacity: 0.28,
        opacity: 0.82,
        weight: 1.3
      }}).addTo(map);
      marker.bindPopup(
        `<strong>${{point.station_name}}</strong><br>${{point.count}} rides<br>` +
        `Avg duration: ${{point.avg_duration_min.toFixed(1)}} min<br>` +
        `Total spend: $${{point.total_cost.toFixed(2)}}`
      );
    }});
  </script>
</body>
</html>
"""


def build_flows_html(title: str, subtitle: str, flows: list[dict]) -> str:
    center = map_center_from_points(
        [(row["start_lat"], row["start_lon"]) for row in flows]
        + [(row["end_lat"], row["end_lon"]) for row in flows]
    )
    payload = json.dumps(flows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; font-family: Helvetica, Arial, sans-serif; }}
    body {{ background: #f3f4f6; }}
    .panel {{
      position: absolute;
      top: 22px;
      left: 22px;
      z-index: 1000;
      background: rgba(255, 255, 255, 0.96);
      padding: 16px 18px;
      border-radius: 22px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.16);
      max-width: 390px;
      border: 1px solid rgba(0, 0, 0, 0.06);
    }}
    .panel h1 {{ font-size: 17px; margin: 0 0 5px; font-weight: 700; letter-spacing: -0.02em; }}
    .panel p {{ font-size: 13px; margin: 0; line-height: 1.4; color: #5f6368; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <h1>{title}</h1>
    <p>{subtitle}</p>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const flows = {payload};
    const map = L.map('map').setView([{center[0]}, {center[1]}], 12);
    L.tileLayer('{BASEMAP_TILE_URL}', {{
      maxZoom: 19,
      subdomains: 'abcd',
      attribution: '{BASEMAP_ATTRIBUTION}'
    }}).addTo(map);
    if (flows.length) {{
      const bounds = L.latLngBounds(
        flows.flatMap((flow) => [[flow.start_lat, flow.start_lon], [flow.end_lat, flow.end_lon]])
      );
      map.fitBounds(bounds.pad(0.08));
    }}

    flows.forEach((flow) => {{
      const line = L.polyline(
        [[flow.start_lat, flow.start_lon], [flow.end_lat, flow.end_lon]],
        {{
          color: '#005f73',
          weight: flow.line_weight,
          opacity: 0.3
        }}
      ).addTo(map);
      line.bindPopup(
        `<strong>${{flow.start_station}}</strong> &rarr; <strong>${{flow.end_station}}</strong><br>` +
        `${{flow.count}} rides<br>` +
        `Avg duration: ${{flow.avg_duration_min.toFixed(1)}} min<br>` +
        `Total spend: $${{flow.total_cost.toFixed(2)}}`
      );
    }});
  </script>
</body>
</html>
"""


def build_all_rides_html(title: str, subtitle: str, rides: list[dict]) -> str:
    center = map_center_from_points(
        [(row["start_lat"], row["start_lon"]) for row in rides]
        + [(row["end_lat"], row["end_lon"]) for row in rides]
    )
    payload = json.dumps(rides)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; font-family: Helvetica, Arial, sans-serif; }}
    body {{ background: #f3f4f6; }}
    .panel {{
      position: absolute;
      top: 22px;
      left: 22px;
      z-index: 1000;
      background: rgba(255, 255, 255, 0.96);
      padding: 16px 18px;
      border-radius: 22px;
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.16);
      max-width: 420px;
      border: 1px solid rgba(0, 0, 0, 0.06);
    }}
    .panel h1 {{ font-size: 17px; margin: 0 0 5px; font-weight: 700; letter-spacing: -0.02em; }}
    .panel p {{ font-size: 13px; margin: 0; line-height: 1.4; color: #5f6368; }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel">
    <h1>{title}</h1>
    <p>{subtitle}</p>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const rides = {payload};
    const map = L.map('map').setView([{center[0]}, {center[1]}], 12);
    L.tileLayer('{BASEMAP_TILE_URL}', {{
      maxZoom: 19,
      subdomains: 'abcd',
      attribution: '{BASEMAP_ATTRIBUTION}'
    }}).addTo(map);
    if (rides.length) {{
      const bounds = L.latLngBounds(
        rides.flatMap((ride) => [[ride.start_lat, ride.start_lon], [ride.end_lat, ride.end_lon]])
      );
      map.fitBounds(bounds.pad(0.08));
    }}

    rides.forEach((ride) => {{
      const line = L.polyline(
        [[ride.start_lat, ride.start_lon], [ride.end_lat, ride.end_lon]],
        {{
          color: ride.color,
          weight: 2,
          opacity: 0.12
        }}
      ).addTo(map);
      line.bindPopup(
        `<strong>${{ride.date}}</strong><br>` +
        `${{ride.start_station}} &rarr; ${{ride.end_station}}<br>` +
        `Duration: ${{ride.duration_min.toFixed(1)}} min<br>` +
        `Cost: $${{ride.total_cost.toFixed(2)}}`
      );
    }});
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    input_csv = Path(args.input_csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    rides = read_rides(input_csv)
    stations = load_stations()
    normalized_lookup, _ = build_station_indexes(stations)
    normalized_names = list(normalized_lookup.keys())

    start_counter: Counter[str] = Counter()
    end_counter: Counter[str] = Counter()
    station_metrics: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "starts": 0.0,
            "ends": 0.0,
            "start_duration_min": 0.0,
            "end_duration_min": 0.0,
            "start_total_cost": 0.0,
            "end_total_cost": 0.0,
        }
    )
    flow_metrics: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "duration_min": 0.0, "total_cost": 0.0}
    )
    month_metrics: dict[str, dict[str, float]] = defaultdict(
        lambda: {"ride_count": 0.0, "duration_min": 0.0, "total_cost": 0.0}
    )

    unique_station_names = {
        clean_station_name(row.get("start_station")) for row in rides if row.get("start_station")
    } | {
        clean_station_name(row.get("end_station")) for row in rides if row.get("end_station")
    }

    match_cache: dict[str, MatchResult] = {}
    for station_name in sorted(name for name in unique_station_names if name):
        match_cache[station_name] = match_station(
            station_name,
            normalized_lookup=normalized_lookup,
            normalized_names=normalized_names,
            fuzzy_cutoff=args.fuzzy_cutoff,
        )

    matched_rides = 0
    matched_ride_rows = []
    for row in rides:
        start_name = clean_station_name(row.get("start_station"))
        end_name = clean_station_name(row.get("end_station"))
        start_match = match_cache.get(start_name, MatchResult(None, "blank", 0.0))
        end_match = match_cache.get(end_name, MatchResult(None, "blank", 0.0))
        duration_min = parse_float(row.get("duration_min"))
        total_cost = parse_float(row.get("total"))
        month = parse_month(row.get("date"))

        month_metrics[month]["ride_count"] += 1
        month_metrics[month]["duration_min"] += duration_min
        month_metrics[month]["total_cost"] += total_cost

        if start_match.station:
            station = start_match.station
            start_counter[station.station_id] += 1
            station_metrics[station.station_id]["starts"] += 1
            station_metrics[station.station_id]["start_duration_min"] += duration_min
            station_metrics[station.station_id]["start_total_cost"] += total_cost

        if end_match.station:
            station = end_match.station
            end_counter[station.station_id] += 1
            station_metrics[station.station_id]["ends"] += 1
            station_metrics[station.station_id]["end_duration_min"] += duration_min
            station_metrics[station.station_id]["end_total_cost"] += total_cost

        if start_match.station and end_match.station:
            matched_rides += 1
            key = (start_match.station.station_id, end_match.station.station_id)
            flow_metrics[key]["count"] += 1
            flow_metrics[key]["duration_min"] += duration_min
            flow_metrics[key]["total_cost"] += total_cost
            matched_ride_rows.append(
                {
                    "date": row.get("date", ""),
                    "start_station": start_match.station.name,
                    "end_station": end_match.station.name,
                    "start_lat": start_match.station.lat,
                    "start_lon": start_match.station.lon,
                    "end_lat": end_match.station.lat,
                    "end_lon": end_match.station.lon,
                    "duration_min": duration_min,
                    "total_cost": total_cost,
                    "color": "#0a9396",
                }
            )

    stations_by_id = {station.station_id: station for station in stations}

    top_station_rows = []
    for station_id, metrics in station_metrics.items():
        total_activity = int(metrics["starts"] + metrics["ends"])
        start_count = int(metrics["starts"])
        end_count = int(metrics["ends"])
        avg_start_duration = metrics["start_duration_min"] / start_count if start_count else 0.0
        avg_end_duration = metrics["end_duration_min"] / end_count if end_count else 0.0
        top_station_rows.append(
            {
                "station_id": station_id,
                "station_name": stations_by_id[station_id].name,
                "lat": f"{stations_by_id[station_id].lat:.6f}",
                "lon": f"{stations_by_id[station_id].lon:.6f}",
                "start_count": start_count,
                "end_count": end_count,
                "total_activity": total_activity,
                "avg_start_duration_min": f"{avg_start_duration:.2f}",
                "avg_end_duration_min": f"{avg_end_duration:.2f}",
                "total_start_cost": f"{metrics['start_total_cost']:.2f}",
                "total_end_cost": f"{metrics['end_total_cost']:.2f}",
            }
        )
    top_station_rows.sort(key=lambda row: (-row["total_activity"], -row["start_count"], row["station_name"]))

    monthly_rows = []
    for month, metrics in sorted(month_metrics.items()):
        ride_count = int(metrics["ride_count"])
        avg_duration = metrics["duration_min"] / ride_count if ride_count else 0.0
        avg_cost = metrics["total_cost"] / ride_count if ride_count else 0.0
        monthly_rows.append(
            {
                "month": month,
                "ride_count": ride_count,
                "total_duration_min": f"{metrics['duration_min']:.2f}",
                "avg_duration_min": f"{avg_duration:.2f}",
                "total_cost": f"{metrics['total_cost']:.2f}",
                "avg_cost": f"{avg_cost:.2f}",
            }
        )

    station_match_rows = []
    unmatched_rows = []
    for source_name, result in sorted(match_cache.items()):
        row = {
            "source_station_name": source_name,
            "matched_station_name": result.station.name if result.station else "",
            "matched_station_id": result.station.station_id if result.station else "",
            "match_method": result.method,
            "match_score": f"{result.score:.4f}",
            "lat": f"{result.station.lat:.6f}" if result.station else "",
            "lon": f"{result.station.lon:.6f}" if result.station else "",
        }
        station_match_rows.append(row)
        if not result.station:
            unmatched_rows.append(row)

    start_points = []
    for station_id, count in start_counter.most_common():
        station = stations_by_id[station_id]
        metrics = station_metrics[station_id]
        avg_duration = metrics["start_duration_min"] / metrics["starts"] if metrics["starts"] else 0.0
        start_points.append(
            {
                "station_name": station.name,
                "lat": station.lat,
                "lon": station.lon,
                "count": count,
                "avg_duration_min": avg_duration,
                "total_cost": metrics["start_total_cost"],
                "color": "#d62828",
            }
        )

    end_points = []
    for station_id, count in end_counter.most_common():
        station = stations_by_id[station_id]
        metrics = station_metrics[station_id]
        end_points.append(
            {
                "station_name": station.name,
                "lat": station.lat,
                "lon": station.lon,
                "count": count,
                "avg_duration_min": metrics["end_duration_min"] / metrics["ends"] if metrics["ends"] else 0.0,
                "total_cost": metrics["end_total_cost"],
                "color": "#1d3557",
            }
        )

    all_points = []
    for row in top_station_rows:
        all_points.append(
            {
                "station_name": row["station_name"],
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "count": int(row["total_activity"]),
                "avg_duration_min": float(row["avg_start_duration_min"]),
                "total_cost": float(row["total_start_cost"]) + float(row["total_end_cost"]),
                "color": "#2a9d8f",
            }
        )

    max_flow_count = max((metrics["count"] for metrics in flow_metrics.values()), default=1)
    flow_rows = []
    for (start_station_id, end_station_id), metrics in sorted(
        flow_metrics.items(), key=lambda item: (-item[1]["count"], item[0][0], item[0][1])
    )[: args.max_flow_lines]:
        start_station = stations_by_id[start_station_id]
        end_station = stations_by_id[end_station_id]
        count = int(metrics["count"])
        flow_rows.append(
            {
                "start_station": start_station.name,
                "end_station": end_station.name,
                "start_lat": start_station.lat,
                "start_lon": start_station.lon,
                "end_lat": end_station.lat,
                "end_lon": end_station.lon,
                "count": count,
                "avg_duration_min": metrics["duration_min"] / count if count else 0.0,
                "total_cost": metrics["total_cost"],
                "line_weight": round(1.5 + 7.0 * math.sqrt(count / max_flow_count), 2),
            }
        )

    write_csv(
        output_dir / "top_stations.csv",
        [
            "station_id",
            "station_name",
            "lat",
            "lon",
            "start_count",
            "end_count",
            "total_activity",
            "avg_start_duration_min",
            "avg_end_duration_min",
            "total_start_cost",
            "total_end_cost",
        ],
        top_station_rows,
    )
    write_csv(
        output_dir / "monthly_stats.csv",
        ["month", "ride_count", "total_duration_min", "avg_duration_min", "total_cost", "avg_cost"],
        monthly_rows,
    )
    write_csv(
        output_dir / "station_matches.csv",
        ["source_station_name", "matched_station_name", "matched_station_id", "match_method", "match_score", "lat", "lon"],
        station_match_rows,
    )
    write_csv(
        output_dir / "unmatched_stations.csv",
        ["source_station_name", "matched_station_name", "matched_station_id", "match_method", "match_score", "lat", "lon"],
        unmatched_rows,
    )

    (output_dir / "starts_map.html").write_text(
        build_heatmap_html(
            title="Citi Bike Start Stations",
            subtitle=f"{len(start_points)} matched origin stations across {len(rides)} rides.",
            points=start_points,
        ),
        encoding="utf-8",
    )
    (output_dir / "ends_map.html").write_text(
        build_heatmap_html(
            title="Citi Bike End Stations",
            subtitle=f"{len(end_points)} matched destination stations across {len(rides)} rides.",
            points=end_points,
        ),
        encoding="utf-8",
    )
    (output_dir / "all_rides_heatmap.html").write_text(
        build_heatmap_html(
            title="All Citi Bike Ride Activity",
            subtitle=f"Combined start and end activity across {len(rides)} rides.",
            points=all_points,
        ),
        encoding="utf-8",
    )
    (output_dir / "flows_map.html").write_text(
        build_flows_html(
            title="Citi Bike Route Flows",
            subtitle=f"Top {len(flow_rows)} matched station-to-station flows across {matched_rides} rides.",
            flows=flow_rows,
        ),
        encoding="utf-8",
    )
    (output_dir / "all_rides_map.html").write_text(
        build_all_rides_html(
            title="All Citi Bike Rides",
            subtitle=f"Each line is one matched ride. Showing {matched_rides} rides from your history.",
            rides=matched_ride_rows,
        ),
        encoding="utf-8",
    )

    summary = {
        "input_csv": str(input_csv),
        "output_dir": str(output_dir),
        "ride_count": len(rides),
        "matched_rides_for_flows": matched_rides,
        "unique_station_names": len(unique_station_names),
        "unmatched_station_names": len(unmatched_rows),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
