from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyperclip


OUTPUT_FILE = Path("market_data.json")
POLL_INTERVAL_SECONDS = 0.4

EXPECTED_HEADERS = [
    "Item",
    "Tier",
    "Day Markup",
    "Day Sales",
    "Week Markup",
    "Week Sales",
    "Month Markup",
    "Month Sales",
    "Year Markup",
    "Year Sales",
    "Decade Markup",
    "Decade Sales",
]

PERIODS = ("day", "week", "month", "year", "decade")

VALUE_WITH_UNIT_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<number>[+-]?\d+(?:[.,]\d+)?)
    (?P<suffix>[KkMm]?)
    (?:\s*(?P<unit>PEC|PED))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_clipboard_text(text: str) -> str:
    text = text.strip().replace("\r\n", "\n").replace("\r", "\n")

    # Some applications copy the complete table surrounded by quotation marks.
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]

    return text.strip()


def parse_decimal(text: str) -> Decimal:
    normalized = text.strip().replace(",", ".")

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid number: {text!r}") from exc


def decimal_to_json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)

    return float(value)


def apply_suffix(value: Decimal, suffix: str) -> Decimal:
    suffix = suffix.upper()

    if suffix == "K":
        return value * Decimal("1000")
    if suffix == "M":
        return value * Decimal("1000000")

    return value


def parse_number_with_optional_unit(text: str) -> tuple[Decimal, str | None]:
    match = VALUE_WITH_UNIT_PATTERN.fullmatch(text)

    if not match:
        raise ValueError(f"Invalid market value: {text!r}")

    value = parse_decimal(match.group("number"))
    value = apply_suffix(value, match.group("suffix") or "")
    unit = match.group("unit")

    return value, unit.upper() if unit else None


def parse_markup(text: str) -> dict[str, Any]:
    """
    Percentage items:
        166.670% -> percentage markup.

    Fixed-markup items:
        5260.300 -> TT+ 5260.300 PED.

    N/A:
        no recorded markup.
    """
    value = text.strip()

    if not value or value.upper() == "N/A":
        return {
            "type": None,
            "value": None,
            "unit": None,
            "raw": value or None,
        }

    if value.endswith("%"):
        percentage = parse_decimal(value[:-1])

        return {
            "type": "percentage",
            "value": decimal_to_json_number(percentage),
            "unit": "percent",
            "raw": value,
        }

    fixed_markup, explicit_unit = parse_number_with_optional_unit(value)

    # Entropia's fixed markup display represents TT + an absolute PED amount.
    if explicit_unit == "PEC":
        fixed_markup /= Decimal("100")
    elif explicit_unit not in (None, "PED"):
        raise ValueError(f"Unsupported fixed markup unit: {explicit_unit!r}")

    return {
        "type": "fixed",
        "value": decimal_to_json_number(fixed_markup),
        "unit": "PED",
        "raw": value,
    }


def parse_sales(text: str, markup_type: str | None) -> dict[str, Any]:
    """
    For percentage-markup items, sales are copied as PED/PEC turnover:
        54.800 PEC -> 0.548 PED
        1.200K PED -> 1200 PED

    For fixed-markup items, sales are copied without a currency:
        2.000 -> 2 sold items
        203.000 -> 203 sold items
    """
    value = text.strip()

    if not value or value.upper() == "N/A":
        return {
            "type": None,
            "value": None,
            "unit": None,
            "raw": value or None,
        }

    amount, explicit_unit = parse_number_with_optional_unit(value)

    if explicit_unit == "PEC":
        amount /= Decimal("100")

        return {
            "type": "turnover",
            "value": decimal_to_json_number(amount),
            "unit": "PED",
            "raw": value,
        }

    if explicit_unit == "PED":
        return {
            "type": "turnover",
            "value": decimal_to_json_number(amount),
            "unit": "PED",
            "raw": value,
        }

    if markup_type == "fixed":
        return {
            "type": "quantity",
            "value": decimal_to_json_number(amount),
            "unit": "items",
            "raw": value,
        }

    # Keep unexpected unitless values usable rather than silently treating
    # them as PED.
    return {
        "type": "unknown",
        "value": decimal_to_json_number(amount),
        "unit": None,
        "raw": value,
    }


def parse_tier(text: str) -> int | float | str:
    try:
        return decimal_to_json_number(parse_decimal(text))
    except ValueError:
        return text.strip()


def split_clipboard_rows(text: str) -> list[list[str]]:
    """
    Entropia normally copies a tab-separated table.

    Item names contain spaces, so a completely flattened space-separated
    string cannot be parsed reliably. Tabs must still be present in the
    actual clipboard data.
    """
    normalized = normalize_clipboard_text(text)

    if "\t" not in normalized:
        return []

    reader = csv.reader(io.StringIO(normalized), delimiter="\t")
    return [
        [cell.strip() for cell in row]
        for row in reader
        if any(cell.strip() for cell in row)
    ]


def parse_market_clipboard(text: str) -> list[dict[str, Any]]:
    rows = split_clipboard_rows(text)

    if len(rows) < 2:
        return []

    headers = rows[0]

    # Remove a possible UTF-8 BOM from the first field.
    if headers:
        headers[0] = headers[0].lstrip("\ufeff")

    if headers != EXPECTED_HEADERS:
        return []

    captured_at = now_iso()
    results: list[dict[str, Any]] = []

    for row_index, row in enumerate(rows[1:], start=2):
        if len(row) != len(EXPECTED_HEADERS):
            raise ValueError(
                f"Row {row_index} has {len(row)} fields; "
                f"expected {len(EXPECTED_HEADERS)}."
            )

        fields = dict(zip(EXPECTED_HEADERS, row, strict=True))

        if not fields["Item"]:
            raise ValueError(f"Row {row_index} has no item name.")

        result: dict[str, Any] = {
            "item": fields["Item"],
            "tier": parse_tier(fields["Tier"]),
            "captured_at": captured_at,
            "periods": {},
        }

        detected_markup_types: set[str] = set()

        for period in PERIODS:
            label = period.capitalize()
            markup = parse_markup(fields[f"{label} Markup"])

            if markup["type"]:
                detected_markup_types.add(markup["type"])

            sales = parse_sales(
                fields[f"{label} Sales"],
                markup_type=markup["type"],
            )

            result["periods"][period] = {
                "markup": markup,
                "sales": sales,
            }

        if detected_markup_types == {"percentage"}:
            result["market_format"] = "percentage"
        elif detected_markup_types == {"fixed"}:
            result["market_format"] = "fixed"
        elif not detected_markup_types:
            result["market_format"] = None
        else:
            result["market_format"] = "mixed"

        results.append(result)

    return results


def empty_database() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "updated_at": None,
        "items": {},
    }


def load_database(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_database()

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{path} contains invalid JSON. Rename or repair it first."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")

    data.setdefault("schema_version", 2)
    data.setdefault("updated_at", None)
    data.setdefault("items", {})

    if not isinstance(data["items"], dict):
        raise RuntimeError(f'{path}: "items" must be a JSON object.')

    return data


def save_database_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    temporary_path.replace(path)


def item_key(snapshot: dict[str, Any]) -> str:
    return f'{snapshot["item"]}::tier={snapshot["tier"]}'


def comparable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key != "captured_at"
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    serialized = json.dumps(
        comparable_snapshot(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(serialized).hexdigest()


def add_snapshot(database: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    key = item_key(snapshot)

    entry = database["items"].setdefault(
        key,
        {
            "item": snapshot["item"],
            "tier": snapshot["tier"],
            "market_format": snapshot["market_format"],
            "snapshots": [],
        },
    )

    snapshots = entry.setdefault("snapshots", [])

    # Ignore only an unchanged latest snapshot for the same item.
    if snapshots and snapshot_hash(snapshots[-1]) == snapshot_hash(snapshot):
        return False

    entry["market_format"] = snapshot["market_format"]
    snapshots.append(snapshot)
    database["updated_at"] = snapshot["captured_at"]

    return True


def run() -> None:
    print("Entropia Market Clipboard Collector")
    print(f"Saving to: {OUTPUT_FILE.resolve()}")
    print("Copy an item's market table in-game.")
    print("Press Ctrl+C to stop.\n")

    try:
        database = load_database(OUTPUT_FILE)
    except RuntimeError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    previous_clipboard: str | None = None

    while True:
        try:
            clipboard = pyperclip.paste()
        except pyperclip.PyperclipException as exc:
            print(f"Clipboard error: {exc}", file=sys.stderr)
            time.sleep(2)
            continue

        if isinstance(clipboard, str) and clipboard != previous_clipboard:
            previous_clipboard = clipboard

            try:
                snapshots = parse_market_clipboard(clipboard)
            except ValueError as exc:
                print(f"Recognized market table but failed to parse it: {exc}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            for snapshot in snapshots:
                if add_snapshot(database, snapshot):
                    save_database_atomic(OUTPUT_FILE, database)
                    print(
                        f'[{snapshot["captured_at"]}] Saved '
                        f'{snapshot["item"]} '
                        f'(tier {snapshot["tier"]}, '
                        f'{snapshot["market_format"]})'
                    )
                else:
                    print(
                        f'Ignored unchanged copy: '
                        f'{snapshot["item"]} (tier {snapshot["tier"]})'
                    )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nCollector stopped.")
