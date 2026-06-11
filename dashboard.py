#!/usr/bin/env python3
"""Copilot usage and cost dashboard."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

DEFAULT_CSV = Path("output/copilot_usage_events.csv")
STATE_FILE = Path("output/dashboard_state.json")
LOCAL_TZ = datetime.now().astimezone().tzinfo
LOCAL_TZ_LABEL = str(LOCAL_TZ) if LOCAL_TZ is not None else "local"
TIME_PRESET_OPTIONS = ["All", "Last 24 hours", "Last 7 days", "Last 30 days", "Custom"]
INTERVAL_TO_PANDAS_FREQ = {
    "5 minutes": "5min",
    "15 minutes": "15min",
    "1 hour": "1h",
    "1 day": "1d",
}


def run_extraction_and_show_progress(csv_path: Path) -> bool:
    project_root = Path(__file__).resolve().parent
    extractor = project_root / "scripts" / "extract_copilot_usage.py"

    if not extractor.exists():
        st.error(f"Extractor not found: {extractor}")
        return False

    cmd = [
        "uv",
        "run",
        "python",
        str(extractor),
        "--output-csv",
        str(csv_path),
        "--progress",
    ]

    progress = st.progress(0, text="Starting extraction...")
    status = st.empty()
    log_lines: list[str] = []
    log_box = st.expander("Extraction logs", expanded=False)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        st.error(f"Failed to start extraction: {exc}")
        return False

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        if line.startswith("PROGRESS|"):
            parts = line.split("|", 4)
            if len(parts) >= 4:
                stage = parts[1]
                current = int(parts[2]) if parts[2].isdigit() else 0
                total = int(parts[3]) if parts[3].isdigit() else 0
                frac = (current / total) if total > 0 else 0.0
                if stage == "scan_start":
                    progress.progress(0, text=f"Scanning {total} log files...")
                elif stage == "scan_file":
                    progress.progress(
                        min(max(frac, 0.0), 1.0),
                        text=f"Processed {current}/{total} log files",
                    )
                elif stage == "done":
                    progress.progress(1.0, text="Finalizing output...")
            continue

        log_lines.append(line)
        status.text(line)
        with log_box:
            st.text("\n".join(log_lines[-20:]))

    return_code = proc.wait()
    if return_code != 0:
        st.error("Extraction failed. See logs above.")
        return False

    progress.progress(1.0, text="Extraction finished")
    st.success("Extraction refreshed successfully.")
    load_usage_data.clear()
    return True


def load_saved_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        # Dashboard should keep working even if state file cannot be written.
        pass


@st.cache_data
def load_usage_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if df.empty:
        return df

    if "project_name" not in df.columns:
        df["project_name"] = "None"
    else:
        df["project_name"] = df["project_name"].fillna("None").astype(str)

    if "project_path" not in df.columns:
        df["project_path"] = "None"
    else:
        df["project_path"] = df["project_path"].fillna("None").astype(str)

    df["ts_iso"] = pd.to_datetime(df["ts_iso"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts_iso"]).copy()
    # Convert to local wall-clock time for filtering and visualization.
    df["ts_local"] = df["ts_iso"].dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)

    numeric_cols = [
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "copilot_usage_nano_aiu",
        "credits",
        "usd_estimate_1c_per_credit",
        "dur_ms",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["requests"] = 1
    return df


def default_start_for_preset(
    preset: str, max_ts: pd.Timestamp, min_ts: pd.Timestamp
) -> pd.Timestamp:
    if preset == "Last 24 hours":
        return max(max_ts - timedelta(hours=24), min_ts)
    if preset == "Last 7 days":
        return max(max_ts - timedelta(days=7), min_ts)
    if preset == "Last 30 days":
        return max(max_ts - timedelta(days=30), min_ts)
    return min_ts


def filter_data_by_time(
    df: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp
) -> pd.DataFrame:
    mask = (df["ts_local"] >= start_ts) & (df["ts_local"] <= end_ts)
    return df.loc[mask].copy()


def aggregate_time_series(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    ts = (
        df.set_index("ts_local")
        .resample(freq)
        .agg(
            requests=("requests", "sum"),
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            cached_tokens=("cached_tokens", "sum"),
            credits=("credits", "sum"),
            usd_estimate_1c_per_credit=("usd_estimate_1c_per_credit", "sum"),
        )
        .reset_index()
    )
    return ts


def main() -> None:
    st.set_page_config(page_title="Copilot Cost Dashboard", layout="wide")
    st.title("Copilot Usage and Cost Over Time")
    saved_state = load_saved_state()

    project_root = Path(__file__).resolve().parent
    csv_file = project_root / DEFAULT_CSV
    st.sidebar.caption(f"CSV source: {csv_file}")
    refresh_clicked = st.sidebar.button(
        "Refresh extraction now", use_container_width=True
    )
    if refresh_clicked:
        if run_extraction_and_show_progress(csv_file):
            st.rerun()

    if not csv_file.exists():
        st.error(f"CSV not found: {csv_file}")
        st.info("Run scripts/extract_copilot_usage.py first to generate the CSV.")
        return

    st.sidebar.download_button(
        "Download usage CSV",
        data=csv_file.read_bytes(),
        file_name=csv_file.name,
        mime="text/csv",
        use_container_width=True,
    )

    try:
        df = load_usage_data(str(csv_file))
    except Exception as exc:
        st.error(f"Failed to load CSV: {exc}")
        return

    if df.empty:
        st.warning("No usage rows found in CSV.")
        return

    max_ts = df["ts_local"].max()
    min_ts = df["ts_local"].min()

    st.sidebar.subheader("Filters")
    preset_default = saved_state.get("preset", "Last 7 days")
    if preset_default not in TIME_PRESET_OPTIONS:
        preset_default = "Last 7 days"
    preset = st.sidebar.selectbox(
        "Time range preset",
        options=TIME_PRESET_OPTIONS,
        index=TIME_PRESET_OPTIONS.index(preset_default),
    )

    if preset == "Custom":
        saved_start_raw = saved_state.get("custom_start")
        saved_end_raw = saved_state.get("custom_end")
        default_start = max(min_ts, max_ts - timedelta(days=7))
        default_end = max_ts
        try:
            saved_start = (
                pd.Timestamp(saved_start_raw) if saved_start_raw else default_start
            )
            saved_end = pd.Timestamp(saved_end_raw) if saved_end_raw else default_end
        except Exception:
            saved_start = default_start
            saved_end = default_end

        slider_start = max(min_ts, min(saved_start, max_ts))
        slider_end = max(min_ts, min(saved_end, max_ts))
        if slider_start > slider_end:
            slider_start, slider_end = slider_end, slider_start

        start_ts, end_ts = st.sidebar.slider(
            f"Select time range ({LOCAL_TZ_LABEL})",
            min_value=min_ts.to_pydatetime(),
            max_value=max_ts.to_pydatetime(),
            value=(slider_start.to_pydatetime(), slider_end.to_pydatetime()),
            format="YYYY-MM-DD HH:mm",
        )
        start_ts = pd.Timestamp(start_ts)
        end_ts = pd.Timestamp(end_ts)
    else:
        start_ts = default_start_for_preset(preset, max_ts, min_ts)
        end_ts = max_ts

    models = sorted(m for m in df["model"].dropna().unique().tolist() if m)
    selected_models = st.sidebar.multiselect(
        "Model filter", options=models, default=models
    )
    project_names = sorted(
        p for p in df["project_name"].dropna().unique().tolist() if p
    )
    selected_projects = st.sidebar.multiselect(
        "Project filter", options=project_names, default=project_names
    )

    interval_options = list(INTERVAL_TO_PANDAS_FREQ.keys())
    interval_default = saved_state.get("interval_label", "1 hour")
    if interval_default not in interval_options:
        interval_default = "1 hour"
    interval_label = st.sidebar.selectbox(
        "Aggregation interval",
        options=interval_options,
        index=interval_options.index(interval_default),
    )
    freq = INTERVAL_TO_PANDAS_FREQ[interval_label]

    save_state(
        {
            "preset": preset,
            "custom_start": start_ts.isoformat(),
            "custom_end": end_ts.isoformat(),
            "interval_label": interval_label,
        }
    )

    filtered = filter_data_by_time(df, start_ts, end_ts)
    if selected_models:
        filtered = filtered[filtered["model"].isin(selected_models)]
    else:
        filtered = filtered.iloc[0:0]
    if selected_projects:
        filtered = filtered[filtered["project_name"].isin(selected_projects)]
    else:
        filtered = filtered.iloc[0:0]

    st.caption(
        f"Showing {len(filtered)} requests from {start_ts.isoformat()} to {end_ts.isoformat()} ({LOCAL_TZ_LABEL})."
    )

    total_requests = int(filtered["requests"].sum()) if not filtered.empty else 0
    total_credits = float(filtered["credits"].sum()) if not filtered.empty else 0.0
    total_usd = (
        float(filtered["usd_estimate_1c_per_credit"].sum())
        if not filtered.empty
        else 0.0
    )
    total_input_tokens = (
        float(filtered["input_tokens"].sum()) if not filtered.empty else 0.0
    )
    total_output_tokens = (
        float(filtered["output_tokens"].sum()) if not filtered.empty else 0.0
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Requests", f"{total_requests:,}")
    c2.metric("Credits", f"{total_credits:,.2f}")
    c3.metric("Estimated USD", f"${total_usd:,.2f}")
    c4.metric("Input Tokens", f"{int(total_input_tokens):,}")
    c5.metric("Output Tokens", f"{int(total_output_tokens):,}")

    if filtered.empty:
        st.warning("No data in selected range/model filter.")
        return

    time_series = aggregate_time_series(filtered, freq)

    st.subheader("Cost Over Time")
    fig_cost = px.line(
        time_series,
        x="ts_local",
        y="usd_estimate_1c_per_credit",
        labels={
            "usd_estimate_1c_per_credit": "Cost (USD)",
            "ts_local": f"Time ({LOCAL_TZ_LABEL})",
        },
    )
    fig_cost.update_yaxes(tickprefix="$", tickformat=",.4f")
    fig_cost.update_traces(hovertemplate="%{x}<br>Cost: $%{y:,.4f}<extra></extra>")
    st.plotly_chart(fig_cost, use_container_width=True)

    st.subheader("Requests Over Time")
    fig_req = px.bar(
        time_series,
        x="ts_local",
        y="requests",
        labels={"ts_local": f"Time ({LOCAL_TZ_LABEL})"},
    )
    st.plotly_chart(fig_req, use_container_width=True)

    st.subheader("Token Usage Over Time")
    fig_tok = px.line(
        time_series,
        x="ts_local",
        y=["input_tokens", "output_tokens", "cached_tokens"],
        labels={
            "value": "Tokens",
            "ts_local": f"Time ({LOCAL_TZ_LABEL})",
            "variable": "Token type",
        },
    )
    st.plotly_chart(fig_tok, use_container_width=True)

    st.subheader("Raw Events")
    show_cols = [
        "ts_local",
        "project_name",
        "session_id",
        "model",
        "debug_name",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "credits",
        "usd_estimate_1c_per_credit",
    ]
    st.dataframe(
        filtered[show_cols].sort_values("ts_local", ascending=False),
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
