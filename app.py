#!/usr/bin/env python3
"""Interactive Citi Bike dashboard."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean

import pydeck as pdk
import streamlit as st

from build_citibike_maps import (
    MatchResult,
    clean_station_name,
    load_stations,
    match_station,
    normalize_station_name,
    parse_float,
)


DEFAULT_CSV_PATH = "/Users/josephruocco/Downloads/citibike_rides_clean.csv"
DEFAULT_MAP_STYLE = "road"


def parse_ride_date(value: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_ride_time(value: str) -> int | None:
    try:
        return datetime.strptime(value.strip(), "%I:%M %p").hour
    except ValueError:
        return None


@st.cache_data(show_spinner=False)
def read_rides(csv_path: str) -> list[dict]:
    path = Path(csv_path).expanduser().resolve()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    cleaned = []
    for row in rows:
        ride_date = parse_ride_date(row.get("date", ""))
        duration_min = parse_float(row.get("duration_min"))
        total_cost = parse_float(row.get("total"))
        start_name = clean_station_name(row.get("start_station"))
        end_name = clean_station_name(row.get("end_station"))
        start_hour = parse_ride_time(row.get("start_time", ""))
        cleaned.append(
            {
                "raw": row,
                "ride_date": ride_date,
                "month": ride_date.strftime("%Y-%m") if ride_date else "unknown",
                "weekday": ride_date.strftime("%A") if ride_date else "Unknown",
                "is_weekend": ride_date.weekday() >= 5 if ride_date else False,
                "start_hour": start_hour,
                "duration_min": duration_min,
                "total_cost": total_cost,
                "cost_per_min": total_cost / duration_min if duration_min > 0 else 0.0,
                "start_station_raw": start_name,
                "end_station_raw": end_name,
            }
        )
    return cleaned


@st.cache_data(show_spinner="Loading Citi Bike station feed...")
def build_dashboard_rows(csv_path: str) -> list[dict]:
    rides = read_rides(csv_path)
    stations = load_stations()
    normalized_lookup: dict[str, list] = defaultdict(list)
    for station in stations:
        normalized_lookup[normalize_station_name(station.name)].append(station)
    normalized_names = list(normalized_lookup.keys())

    unique_names = {
        row["start_station_raw"] for row in rides if row["start_station_raw"]
    } | {row["end_station_raw"] for row in rides if row["end_station_raw"]}

    match_cache: dict[str, MatchResult] = {}
    for name in sorted(unique_names):
        match_cache[name] = match_station(name, normalized_lookup, normalized_names, fuzzy_cutoff=0.84)

    matched_rows = []
    for row in rides:
        start_match = match_cache.get(row["start_station_raw"])
        end_match = match_cache.get(row["end_station_raw"])
        start_station = start_match.station if start_match else None
        end_station = end_match.station if end_match else None
        matched_rows.append(
            {
                **row,
                "start_station_name": start_station.name if start_station else row["start_station_raw"],
                "end_station_name": end_station.name if end_station else row["end_station_raw"],
                "start_station_id": start_station.station_id if start_station else None,
                "end_station_id": end_station.station_id if end_station else None,
                "start_lat": start_station.lat if start_station else None,
                "start_lon": start_station.lon if start_station else None,
                "end_lat": end_station.lat if end_station else None,
                "end_lon": end_station.lon if end_station else None,
                "start_matched": bool(start_station),
                "end_matched": bool(end_station),
                "flow_matched": bool(start_station and end_station),
            }
        )
    return matched_rows


def filter_rows(rows: list[dict]) -> list[dict]:
    dated_rows = [row for row in rows if row["ride_date"] is not None]
    min_date = min(row["ride_date"] for row in dated_rows) if dated_rows else date.today()
    max_date = max(row["ride_date"] for row in dated_rows) if dated_rows else date.today()
    duration_max = max((row["duration_min"] for row in rows), default=0.0)
    cost_max = max((row["total_cost"] for row in rows), default=0.0)

    st.sidebar.header("Filters")
    date_range = st.sidebar.date_input(
        "Date range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date

    day_mode = st.sidebar.segmented_control(
        "Day type",
        options=["All", "Weekdays", "Weekends"],
        default="All",
    )
    duration_range = st.sidebar.slider(
        "Duration (minutes)",
        min_value=0.0,
        max_value=max(1.0, float(duration_max)),
        value=(0.0, max(1.0, float(duration_max))),
        step=1.0,
    )
    cost_range = st.sidebar.slider(
        "Cost ($)",
        min_value=0.0,
        max_value=max(1.0, round(float(cost_max), 2)),
        value=(0.0, max(1.0, round(float(cost_max), 2))),
        step=0.25,
    )

    months = sorted({row["month"] for row in rows if row["month"] != "unknown"})
    selected_months = st.sidebar.multiselect("Months", options=months, default=months)

    start_options = sorted({row["start_station_name"] for row in rows if row["start_station_name"]})
    end_options = sorted({row["end_station_name"] for row in rows if row["end_station_name"]})
    selected_starts = st.sidebar.multiselect("Start stations", options=start_options)
    selected_ends = st.sidebar.multiselect("End stations", options=end_options)

    matched_only = st.sidebar.toggle("Matched stations only", value=True)
    round_trip_only = st.sidebar.toggle("Round trips only", value=False)

    filtered = []
    for row in rows:
        ride_date = row["ride_date"]
        if ride_date is None or not (start_date <= ride_date <= end_date):
            continue
        if selected_months and row["month"] not in selected_months:
            continue
        if day_mode == "Weekdays" and row["is_weekend"]:
            continue
        if day_mode == "Weekends" and not row["is_weekend"]:
            continue
        if not (duration_range[0] <= row["duration_min"] <= duration_range[1]):
            continue
        if not (cost_range[0] <= row["total_cost"] <= cost_range[1]):
            continue
        if selected_starts and row["start_station_name"] not in selected_starts:
            continue
        if selected_ends and row["end_station_name"] not in selected_ends:
            continue
        if matched_only and not row["flow_matched"]:
            continue
        if round_trip_only and row["start_station_name"] != row["end_station_name"]:
            continue
        filtered.append(row)
    return filtered


def metric_value(value: float) -> str:
    return f"{value:,.2f}"


def build_station_points(rows: list[dict], mode: str) -> list[dict]:
    key_prefix = "start" if mode == "Starts" else "end"
    aggregates: dict[str, dict] = {}
    for row in rows:
        station_id = row.get(f"{key_prefix}_station_id")
        lat = row.get(f"{key_prefix}_lat")
        lon = row.get(f"{key_prefix}_lon")
        name = row.get(f"{key_prefix}_station_name")
        if not station_id or lat is None or lon is None:
            continue
        bucket = aggregates.setdefault(
            station_id,
            {
                "station_name": name,
                "lat": lat,
                "lon": lon,
                "count": 0,
                "duration_values": [],
                "cost_values": [],
            },
        )
        bucket["count"] += 1
        bucket["duration_values"].append(row["duration_min"])
        bucket["cost_values"].append(row["total_cost"])

    color = [18, 147, 199] if mode == "Starts" else [0, 95, 115]
    points = []
    for bucket in aggregates.values():
        avg_duration = mean(bucket["duration_values"]) if bucket["duration_values"] else 0.0
        total_cost = sum(bucket["cost_values"])
        points.append(
            {
                "station_name": bucket["station_name"],
                "lat": bucket["lat"],
                "lon": bucket["lon"],
                "count": bucket["count"],
                "avg_duration_min": avg_duration,
                "total_cost": total_cost,
                "radius": min(320, 40 + bucket["count"] * 14),
                "fill_color": color + [120],
                "line_color": color + [220],
            }
        )
    return points


def build_activity_points(rows: list[dict]) -> list[dict]:
    aggregates: dict[str, dict] = {}
    for row in rows:
        for prefix in ("start", "end"):
            station_id = row.get(f"{prefix}_station_id")
            lat = row.get(f"{prefix}_lat")
            lon = row.get(f"{prefix}_lon")
            name = row.get(f"{prefix}_station_name")
            if not station_id or lat is None or lon is None:
                continue
            bucket = aggregates.setdefault(
                station_id,
                {
                    "station_name": name,
                    "lat": lat,
                    "lon": lon,
                    "count": 0,
                    "duration_values": [],
                    "cost_values": [],
                },
            )
            bucket["count"] += 1
            bucket["duration_values"].append(row["duration_min"])
            bucket["cost_values"].append(row["total_cost"])

    points = []
    for bucket in aggregates.values():
        avg_duration = mean(bucket["duration_values"]) if bucket["duration_values"] else 0.0
        total_cost = sum(bucket["cost_values"])
        points.append(
            {
                "station_name": bucket["station_name"],
                "lat": bucket["lat"],
                "lon": bucket["lon"],
                "count": bucket["count"],
                "avg_duration_min": avg_duration,
                "total_cost": total_cost,
                "radius": min(360, 44 + bucket["count"] * 12),
                "fill_color": [53, 182, 232, 95],
                "line_color": [20, 147, 199, 220],
            }
        )
    return points


def build_flow_rows(rows: list[dict], top_n: int = 200) -> list[dict]:
    aggregates: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row["flow_matched"]:
            continue
        key = (row["start_station_id"], row["end_station_id"])
        bucket = aggregates.setdefault(
            key,
            {
                "start_station": row["start_station_name"],
                "end_station": row["end_station_name"],
                "start_lat": row["start_lat"],
                "start_lon": row["start_lon"],
                "end_lat": row["end_lat"],
                "end_lon": row["end_lon"],
                "count": 0,
                "duration_values": [],
                "cost_values": [],
            },
        )
        bucket["count"] += 1
        bucket["duration_values"].append(row["duration_min"])
        bucket["cost_values"].append(row["total_cost"])

    ranked = sorted(aggregates.values(), key=lambda item: item["count"], reverse=True)[:top_n]
    if not ranked:
        return []
    max_count = max(item["count"] for item in ranked)
    result = []
    for item in ranked:
        result.append(
            {
                **item,
                "avg_duration_min": mean(item["duration_values"]) if item["duration_values"] else 0.0,
                "total_cost": sum(item["cost_values"]),
                "width": 2 + (8 * item["count"] / max_count),
            }
        )
    return result


def build_trip_rows(rows: list[dict]) -> list[dict]:
    trips = []
    for row in rows:
        if not row["flow_matched"]:
            continue
        trips.append(
            {
                "start_station": row["start_station_name"],
                "end_station": row["end_station_name"],
                "date_label": row["ride_date"].isoformat() if row["ride_date"] else "",
                "duration_min": row["duration_min"],
                "total_cost": row["total_cost"],
                "source": [row["start_lon"], row["start_lat"]],
                "target": [row["end_lon"], row["end_lat"]],
            }
        )
    return trips


def compute_view_state(rows: list[dict]) -> pdk.ViewState:
    coords = []
    for row in rows:
        for lat_key, lon_key in (("start_lat", "start_lon"), ("end_lat", "end_lon")):
            lat = row.get(lat_key)
            lon = row.get(lon_key)
            if lat is not None and lon is not None:
                coords.append((lat, lon))
    if not coords:
        return pdk.ViewState(latitude=40.73061, longitude=-73.935242, zoom=11)
    avg_lat = sum(lat for lat, _ in coords) / len(coords)
    avg_lon = sum(lon for _, lon in coords) / len(coords)
    lat_span = max(lat for lat, _ in coords) - min(lat for lat, _ in coords)
    lon_span = max(lon for _, lon in coords) - min(lon for _, lon in coords)
    span = max(lat_span, lon_span)
    if span > 0.6:
        zoom = 10
    elif span > 0.25:
        zoom = 11
    elif span > 0.12:
        zoom = 12
    else:
        zoom = 13
    return pdk.ViewState(latitude=avg_lat, longitude=avg_lon, zoom=zoom, pitch=0)


def render_metrics(rows: list[dict]) -> None:
    total_rides = len(rows)
    total_cost = sum(row["total_cost"] for row in rows)
    avg_duration = mean(row["duration_min"] for row in rows) if rows else 0.0
    avg_cost = total_cost / total_rides if total_rides else 0.0

    start_counts = Counter(row["start_station_name"] for row in rows if row["start_station_name"])
    end_counts = Counter(row["end_station_name"] for row in rows if row["end_station_name"])
    flow_counts = Counter(
        (row["start_station_name"], row["end_station_name"]) for row in rows if row["flow_matched"]
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Rides", f"{total_rides:,}")
    col2.metric("Total spend", f"${metric_value(total_cost)}")
    col3.metric("Avg duration", f"{avg_duration:.1f} min")
    col4.metric("Avg cost", f"${avg_cost:.2f}")

    col5, col6, col7 = st.columns(3)
    col5.metric("Top start", start_counts.most_common(1)[0][0] if start_counts else "N/A")
    col6.metric("Top end", end_counts.most_common(1)[0][0] if end_counts else "N/A")
    if flow_counts:
        top_flow = flow_counts.most_common(1)[0]
        col7.metric("Top route", f"{top_flow[0][0]} -> {top_flow[0][1]}")
    else:
        col7.metric("Top route", "N/A")


def render_map(rows: list[dict], map_mode: str) -> None:
    layers = []
    tooltip = None

    if map_mode == "All activity":
        points = build_activity_points(rows)
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=points,
                get_position="[lon, lat]",
                get_radius="radius",
                get_fill_color="fill_color",
                get_line_color="line_color",
                line_width_min_pixels=1,
                pickable=True,
                stroked=True,
                filled=True,
            )
        )
        tooltip = {
            "html": "<b>{station_name}</b><br/>{count} rides<br/>Avg duration: {avg_duration_min} min<br/>Total spend: ${total_cost}",
            "style": {"backgroundColor": "white", "color": "#111827"},
        }
    elif map_mode in {"Starts", "Ends"}:
        points = build_station_points(rows, map_mode)
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=points,
                get_position="[lon, lat]",
                get_radius="radius",
                get_fill_color="fill_color",
                get_line_color="line_color",
                line_width_min_pixels=1,
                pickable=True,
                stroked=True,
                filled=True,
            )
        )
        tooltip = {
            "html": "<b>{station_name}</b><br/>{count} rides<br/>Avg duration: {avg_duration_min} min<br/>Total spend: ${total_cost}",
            "style": {"backgroundColor": "white", "color": "#111827"},
        }
    elif map_mode == "Top flows":
        flows = build_flow_rows(rows)
        layers.append(
            pdk.Layer(
                "ArcLayer",
                data=flows,
                get_source_position="[start_lon, start_lat]",
                get_target_position="[end_lon, end_lat]",
                get_width="width",
                get_source_color=[20, 147, 199, 180],
                get_target_color=[0, 95, 115, 180],
                pickable=True,
                auto_highlight=True,
            )
        )
        tooltip = {
            "html": "<b>{start_station}</b> -> <b>{end_station}</b><br/>{count} rides<br/>Avg duration: {avg_duration_min} min<br/>Total spend: ${total_cost}",
            "style": {"backgroundColor": "white", "color": "#111827"},
        }
    else:
        trips = build_trip_rows(rows)
        layers.append(
            pdk.Layer(
                "ArcLayer",
                data=trips,
                get_source_position="source",
                get_target_position="target",
                get_width=1.5,
                get_source_color=[53, 182, 232, 60],
                get_target_color=[20, 147, 199, 60],
                pickable=True,
                auto_highlight=True,
            )
        )
        tooltip = {
            "html": "<b>{date_label}</b><br/>{start_station} -> {end_station}<br/>Duration: {duration_min} min<br/>Cost: ${total_cost}",
            "style": {"backgroundColor": "white", "color": "#111827"},
        }

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=compute_view_state(rows),
        map_provider="carto",
        map_style=DEFAULT_MAP_STYLE,
        tooltip=tooltip,
    )
    st.pydeck_chart(deck, use_container_width=True)


def render_tables(rows: list[dict]) -> None:
    start_counts = Counter(row["start_station_name"] for row in rows if row["start_station_name"])
    end_counts = Counter(row["end_station_name"] for row in rows if row["end_station_name"])
    flow_counts = Counter(
        f"{row['start_station_name']} -> {row['end_station_name']}" for row in rows if row["flow_matched"]
    )

    station_col, route_col = st.columns(2)
    station_col.subheader("Top start stations")
    station_col.dataframe(
        [{"station": station, "rides": count} for station, count in start_counts.most_common(10)],
        use_container_width=True,
        hide_index=True,
    )
    route_col.subheader("Top routes")
    route_col.dataframe(
        [{"route": route, "rides": count} for route, count in flow_counts.most_common(10)],
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("Recent rides", expanded=False):
        preview = [
            {
                "date": row["ride_date"].isoformat() if row["ride_date"] else "",
                "start": row["start_station_name"],
                "end": row["end_station_name"],
                "duration_min": row["duration_min"],
                "cost": row["total_cost"],
            }
            for row in sorted(rows, key=lambda item: item["ride_date"] or date.min, reverse=True)[:50]
        ]
        st.dataframe(preview, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Citi Bike Dashboard", layout="wide")
    st.title("Citi Bike Dashboard")
    st.caption("Interactive filters, ride stats, and station/route maps from your exported ride history.")

    csv_path = st.sidebar.text_input("CSV path", value=DEFAULT_CSV_PATH)
    map_mode = st.sidebar.radio(
        "Map mode",
        options=["All activity", "Starts", "Ends", "Top flows", "All rides"],
        index=0,
    )

    try:
        rows = build_dashboard_rows(csv_path)
    except Exception as exc:
        st.error(f"Unable to load ride data: {exc}")
        st.stop()

    filtered_rows = filter_rows(rows)
    if not filtered_rows:
        st.warning("No rides match the current filters.")
        st.stop()

    render_metrics(filtered_rows)
    render_map(filtered_rows, map_mode)
    render_tables(filtered_rows)


if __name__ == "__main__":
    main()
