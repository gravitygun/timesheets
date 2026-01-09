#!/usr/bin/env python3
"""Import timesheet data from extracted Excel JSON."""

import json
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import storage
from models import TimeEntry, Config


def parse_time_value(val: str | None) -> time | None:
    """Parse time from JSON value like '09:15:00' or '2025-08-30 00:00:00'."""
    if not val or val == "00:00:00":
        return None

    # Handle datetime strings (just take the time part if it's midnight, skip it)
    if " " in val and "00:00:00" in val:
        return None

    parts = val.replace(" ", ":").split(":")
    hour, minute = int(parts[0]), int(parts[1])

    # Skip midnight times (usually empty cells)
    if hour == 0 and minute == 0:
        return None

    return time(hour, minute)


def parse_duration(val: str | None) -> timedelta | None:
    """Parse duration from JSON value like '00:30:00'."""
    if not val or val == "00:00:00":
        return None

    parts = val.split(":")
    hours, minutes = int(parts[0]), int(parts[1])

    if hours == 0 and minutes == 0:
        return None

    return timedelta(hours=hours, minutes=minutes)


def parse_date(val: str | None) -> date | None:
    """Parse date from JSON value like '2025-08-30 00:00:00'."""
    if not val:
        return None

    try:
        date_part = val.split(" ")[0]
        parts = date_part.split("-")
        if len(parts) != 3:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def import_sheet(sheet_data: dict) -> list[TimeEntry]:
    """Import a single sheet (month) of data."""
    entries = []

    # Group cells by row number
    rows: dict[int, dict] = {}
    for cell_ref, cell_data in sheet_data.items():
        # Parse row number from cell ref like 'A2', 'B10'
        col = ""
        row_str = ""
        for char in cell_ref:
            if char.isalpha():
                col += char
            else:
                row_str += char

        row_num = int(row_str)

        # Skip header row
        if row_num == 1:
            continue

        if row_num not in rows:
            rows[row_num] = {}
        rows[row_num][col] = cell_data

    for row_num, row_data in sorted(rows.items()):
        # Column mapping:
        # A = day of week, B = date, C = in, D = lunch, E = out
        # F = total (calculated), G = decimal (calculated)
        # H = adjust, I = decimal adjust (calculated), J = adjust type, K = comment

        date_val = row_data.get("B", {}).get("value")
        if not date_val:
            continue

        entry_date = parse_date(date_val)
        if not entry_date:
            continue

        day_of_week = row_data.get("A", {}).get("value", "")

        # Skip summary rows (they don't have day of week like Mon, Tue, etc.)
        if day_of_week not in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            continue

        clock_in = parse_time_value(row_data.get("C", {}).get("value"))
        lunch = parse_duration(row_data.get("D", {}).get("value"))
        clock_out = parse_time_value(row_data.get("E", {}).get("value"))
        adjustment = parse_duration(row_data.get("H", {}).get("value"))
        adjust_type = row_data.get("J", {}).get("value")
        comment = row_data.get("K", {}).get("value")

        entry = TimeEntry(
            date=entry_date,
            day_of_week=day_of_week,
            clock_in=clock_in,
            lunch_duration=lunch,
            clock_out=clock_out,
            adjustment=adjustment,
            adjust_type=adjust_type,
            comment=comment,
        )
        entries.append(entry)

    return entries


def import_from_json(json_path: Path):
    """Import all data from extracted JSON file."""
    with open(json_path) as f:
        data = json.load(f)

    # Initialize database
    storage.init_db()

    # Import config
    config_data = data.get("Config", {})
    hourly_rate = config_data.get("B1", {}).get("value", 97)
    config = Config(hourly_rate=Decimal(str(hourly_rate)), currency="GBP")
    storage.save_config(config)
    print(f"Imported config: £{config.hourly_rate}/hr")

    # Import time entries from monthly sheets
    total_entries = 0
    skip_sheets = {"Config", "Sick 2012-13", "Summary 2012-13"}

    for sheet_name, sheet_data in data.items():
        if sheet_name in skip_sheets:
            continue

        entries = import_sheet(sheet_data)
        for entry in entries:
            storage.save_entry(entry)

        total_entries += len(entries)
        print(f"Imported {len(entries)} entries from {sheet_name}")

    print(f"\nTotal: {total_entries} entries imported")


if __name__ == "__main__":
    json_path = Path(__file__).parent / "spreadsheet_structure.json"
    import_from_json(json_path)
