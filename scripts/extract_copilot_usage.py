#!/usr/bin/env python3
"""Extract Copilot Chat usage metrics from VS Code debug logs.

This script scans all session `main.jsonl` files under the VS Code workspaceStorage
area and emits one CSV row per `llm_request` event, including timestamp fields
for later time-based aggregation.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

DEFAULT_LOG_ROOT = Path.home() / ".vscode-server" / "data" / "User" / "workspaceStorage"
NANO_TO_CREDITS = 1_000_000_000
CREDIT_TO_USD = 0.01
PROJECT_CWD_PATTERN = re.compile(r"Current working directory:\s*(/home/[^\s\\\"']+)")


@dataclass
class UsageRow:
    workspace_storage_id: str
    session_id: str
    project_path: str
    project_name: str
    event_type: str
    event_name: str
    model: str
    debug_name: str
    ts_ms: int | None
    ts_iso: str
    dur_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    copilot_usage_nano_aiu: int | None
    credits: float | None
    usd_estimate_1c_per_credit: float | None


def ms_to_iso_utc(ts_ms: int | None) -> str:
    if not isinstance(ts_ms, int):
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


def iter_main_logs(root: Path) -> Iterable[Path]:
    pattern = "*/GitHub.copilot-chat/debug-logs/*/main.jsonl"
    yield from root.glob(pattern)


def extract_project_from_attrs(attrs: dict) -> tuple[str, str]:
    text_parts: list[str] = []
    for key in ("userRequest", "inputMessages"):
        value = attrs.get(key)
        if isinstance(value, str) and value:
            text_parts.append(value)

    text = "\n".join(text_parts)
    match = PROJECT_CWD_PATTERN.search(text)
    if not match:
        return "None", "None"

    project_path = match.group(1)

    # Filter out unreliable entries
    if not project_path or project_path.endswith(("...", "..", "/")):
        return "None", "None"

    project_name = Path(project_path).name or "None"

    # Filter out home directory or username-only entries
    home_dir = str(Path.home())
    if (
        project_path == home_dir
        or project_name == Path.home().name
        or project_name in (".", "..")
    ):
        return "None", "None"

    return project_path, project_name


def parse_row(log_file: Path, record: dict) -> UsageRow | None:
    if record.get("type") != "llm_request":
        return None

    attrs = record.get("attrs") or {}
    ts_ms = record.get("ts") if isinstance(record.get("ts"), int) else None
    dur_ms = record.get("dur") if isinstance(record.get("dur"), int) else None

    nano = attrs.get("copilotUsageNanoAiu")
    if not isinstance(nano, int):
        nano = None

    credits = (nano / NANO_TO_CREDITS) if nano is not None else None
    usd = (credits * CREDIT_TO_USD) if credits is not None else None

    session_id = log_file.parent.name
    workspace_storage_id = (
        log_file.parents[3].name if len(log_file.parents) >= 4 else ""
    )
    project_path, project_name = extract_project_from_attrs(attrs)

    return UsageRow(
        workspace_storage_id=workspace_storage_id,
        session_id=session_id,
        project_path=project_path,
        project_name=project_name,
        event_type=record.get("type") or "",
        event_name=record.get("name") or "",
        model=attrs.get("model") or "",
        debug_name=attrs.get("debugName") or "",
        ts_ms=ts_ms,
        ts_iso=ms_to_iso_utc(ts_ms),
        dur_ms=dur_ms,
        input_tokens=(
            attrs.get("inputTokens")
            if isinstance(attrs.get("inputTokens"), int)
            else None
        ),
        output_tokens=(
            attrs.get("outputTokens")
            if isinstance(attrs.get("outputTokens"), int)
            else None
        ),
        cached_tokens=(
            attrs.get("cachedTokens")
            if isinstance(attrs.get("cachedTokens"), int)
            else None
        ),
        copilot_usage_nano_aiu=nano,
        credits=credits,
        usd_estimate_1c_per_credit=usd,
    )


def extract_usage_rows(
    root: Path, emit_progress: bool = False
) -> tuple[list[UsageRow], int]:
    rows: list[UsageRow] = []
    parse_errors = 0
    main_logs = list(iter_main_logs(root))
    total_logs = len(main_logs)

    if emit_progress:
        print(f"PROGRESS|scan_start|0|{total_logs}")

    for idx, main_log in enumerate(main_logs, start=1):
        if emit_progress:
            print(f"PROGRESS|scan_file|{idx}|{total_logs}|{main_log}")
        try:
            lines = main_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            parse_errors += 1
            continue

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            row = parse_row(main_log, record)
            if row is not None:
                rows.append(row)

    if emit_progress:
        print(f"PROGRESS|done|{total_logs}|{total_logs}")

    rows.sort(key=lambda r: (r.ts_ms or 0, r.session_id, r.model))
    return rows, parse_errors


def write_csv(rows: list[UsageRow], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(UsageRow.__dataclass_fields__.keys())

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def print_summary(rows: list[UsageRow], parse_errors: int, output_csv: Path) -> None:
    total_nano = sum(r.copilot_usage_nano_aiu or 0 for r in rows)
    total_credits = total_nano / NANO_TO_CREDITS
    total_usd = total_credits * CREDIT_TO_USD
    unique_sessions = len({r.session_id for r in rows})

    print(f"Wrote {len(rows)} llm_request rows to: {output_csv}")
    print(f"Unique sessions: {unique_sessions}")
    print(f"Total nano AIU: {total_nano}")
    print(f"Total credits: {total_credits:.6f}")
    print(f"Estimated USD (@$0.01/credit): {total_usd:.6f}")
    if parse_errors:
        print(f"Skipped/parse errors: {parse_errors}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Copilot usage metrics from all session logs and include event timestamps "
            "for downstream aggregation."
        )
    )
    parser.add_argument(
        "--log-root",
        type=Path,
        default=DEFAULT_LOG_ROOT,
        help=(
            "Root containing VS Code workspaceStorage directories "
            f"(default: {DEFAULT_LOG_ROOT})"
        ),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("output/copilot_usage_events.csv"),
        help="Path for extracted usage events CSV (default: output/copilot_usage_events.csv)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Emit machine-readable progress lines to stdout for UI consumers.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.log_root.exists():
        parser.error(f"Log root does not exist: {args.log_root}")

    rows, parse_errors = extract_usage_rows(args.log_root, emit_progress=args.progress)
    write_csv(rows, args.output_csv)
    print_summary(rows, parse_errors, args.output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
