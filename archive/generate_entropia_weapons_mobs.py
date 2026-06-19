import argparse
import json
import pprint
from pathlib import Path
from typing import Any

HEADER = """# Auto-generated from Entropia Nexus.
# This file is hardcoded and does not call the internet.

"""


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def unwrap_list(data: Any) -> list[Any]:
    """Supports plain list and common wrapped API responses."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("data", "items", "weapons", "mobs", "results", "value"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    raise ValueError(f"Unsupported JSON shape. Expected list or wrapped list, got {type(data).__name__}")


def first_value(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Case-sensitive first existing key lookup."""
    for key in keys:
        if key in obj:
            return obj[key]
    return default


def deep_first(obj: Any, *paths: tuple[str, ...], default: Any = None) -> Any:
    """Try several nested paths and return the first non-None value."""
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur is not None:
            return cur
    return default


def to_number_or_none(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        try:
            number = float(cleaned)
            return int(number) if number.is_integer() else number
        except ValueError:
            return value
    return value


def write_python_file(path: Path, variable_name: str, data: dict[str, Any]) -> None:
    text = pprint.pformat(data, width=140, sort_dicts=False)
    path.write_text(HEADER + f"{variable_name} = {text}\n", encoding="utf-8")
    print(f"Generated {path} ({len(data)} records)")


def build_weapons(raw_items: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = first_value(item, "Name", "name", "Title", "title")
        if not name:
            continue

        economy = deep_first(
            item,
            ("Properties", "Economy"),
            ("properties", "economy"),
            ("Economy",),
            ("economy",),
            default={},
        )
        if not isinstance(economy, dict):
            economy = {}

        result[str(name)] = {
            "decay": to_number_or_none(first_value(economy, "Decay", "decay")),
            "ammo_burn": to_number_or_none(first_value(economy, "AmmoBurn", "ammoBurn", "ammo_burn")),
        }

    return dict(sorted(result.items(), key=lambda x: x[0].lower()))


def extract_planets(item: dict[str, Any]) -> list[str]:
    candidates = [
        item.get("Planet"),
        item.get("planet"),
        item.get("Planets"),
        item.get("planets"),
        item.get("Locations"),
        item.get("locations"),
        deep_first(item, ("Properties", "Planet")),
        deep_first(item, ("Properties", "Planets")),
        deep_first(item, ("Properties", "Location")),
        deep_first(item, ("Properties", "Locations")),
    ]

    planets: list[str] = []

    def add_planet(value: Any) -> None:
        if value is None or value == "":
            return

        if isinstance(value, str):
            if value not in planets:
                planets.append(value)
            return

        if isinstance(value, dict):
            name = first_value(value, "Name", "name", "Planet", "planet", "Location", "location")
            if name and str(name) not in planets:
                planets.append(str(name))
            return

        if isinstance(value, list):
            for sub in value:
                add_planet(sub)

    for candidate in candidates:
        add_planet(candidate)

    return planets


def extract_maturities(item: dict[str, Any]) -> list[Any]:
    for key in ("Maturities", "maturities", "Maturity", "maturity", "MobMaturities", "mobMaturities"):
        value = item.get(key)
        if isinstance(value, list):
            return value

    props = item.get("Properties") or item.get("properties") or {}
    if isinstance(props, dict):
        for key in ("Maturities", "maturities", "Maturity", "maturity", "MobMaturities", "mobMaturities"):
            value = props.get(key)
            if isinstance(value, list):
                return value

    return []


def maturity_name(maturity: dict[str, Any]) -> str:
    name = first_value(maturity, "Name", "name", "Maturity", "maturity", "Label", "label")
    if isinstance(name, dict):
        nested = first_value(name, "Name", "name")
        if nested:
            return str(nested)
    if name:
        return str(name)
    return "Unknown"


def maturity_hp(maturity: dict[str, Any]) -> Any:
    value = deep_first(
        maturity,
        ("Hp",),
        ("HP",),
        ("Health",),
        ("health",),
        ("Properties", "Hp"),
        ("Properties", "HP"),
        ("Properties", "Health"),
        ("properties", "hp"),
        ("properties", "health"),
    )
    return to_number_or_none(value)


def maturity_level(maturity: dict[str, Any]) -> Any:
    value = deep_first(
        maturity,
        ("Level",),
        ("level",),
        ("ThreatLevel",),
        ("threatLevel",),
        ("Properties", "Level"),
        ("Properties", "ThreatLevel"),
        ("properties", "level"),
        ("properties", "threatLevel"),
    )
    return to_number_or_none(value)


def build_mobs(raw_items: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = first_value(item, "Name", "name", "Title", "title")
        if not name:
            continue

        maturities: dict[str, dict[str, Any]] = {}
        for maturity in extract_maturities(item):
            if not isinstance(maturity, dict):
                continue

            m_name = maturity_name(maturity)
            maturities[m_name] = {
                "hp": maturity_hp(maturity),
                "level": maturity_level(maturity),
            }

        result[str(name)] = {
            "planets": extract_planets(item),
            "maturities": dict(sorted(maturities.items(), key=lambda x: x[0].lower())),
        }

    return dict(sorted(result.items(), key=lambda x: x[0].lower()))


def print_inspect(path: Path, limit: int) -> None:
    items = unwrap_list(read_json(path))
    print(f"{path}: {len(items)} items")
    for index, item in enumerate(items[:limit], start=1):
        print(f"\n--- Item {index} ---")
        if isinstance(item, dict):
            print("Top keys:", list(item.keys()))
            print("Name:", first_value(item, "Name", "name", "Title", "title"))
            props = item.get("Properties") or item.get("properties")
            if isinstance(props, dict):
                print("Properties keys:", list(props.keys()))
            maturities = extract_maturities(item)
            print("Maturities found:", len(maturities))
            if maturities and isinstance(maturities[0], dict):
                print("First maturity keys:", list(maturities[0].keys()))
                mprops = maturities[0].get("Properties") or maturities[0].get("properties")
                if isinstance(mprops, dict):
                    print("First maturity Properties keys:", list(mprops.keys()))
            print("Planets found:", extract_planets(item))
        else:
            print(type(item).__name__, item)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate hardcoded Entropia weapon_data.py and mob_data.py from local JSON files."
    )
    parser.add_argument("--weapons-json", default="weapons.json", help="Path to weapons JSON")
    parser.add_argument("--mobs-json", default="mobs.json", help="Path to mobs JSON")
    parser.add_argument("--out-dir", default=".", help="Output directory")
    parser.add_argument("--weapons-out", default="weapon_data.py", help="Output filename for weapons")
    parser.add_argument("--mobs-out", default="mob_data.py", help="Output filename for mobs")
    parser.add_argument("--skip-weapons", action="store_true")
    parser.add_argument("--skip-mobs", action="store_true")
    parser.add_argument("--inspect", choices=["weapons", "mobs"], help="Print detected JSON shape and exit")
    parser.add_argument("--inspect-limit", type=int, default=3)
    args = parser.parse_args()

    if args.inspect:
        inspect_path = Path(args.weapons_json if args.inspect == "weapons" else args.mobs_json)
        print_inspect(inspect_path, args.inspect_limit)
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_weapons:
        weapons_items = unwrap_list(read_json(Path(args.weapons_json)))
        weapons = build_weapons(weapons_items)
        write_python_file(out_dir / args.weapons_out, "WEAPONS", weapons)

    if not args.skip_mobs:
        mobs_items = unwrap_list(read_json(Path(args.mobs_json)))
        mobs = build_mobs(mobs_items)
        write_python_file(out_dir / args.mobs_out, "MOBS", mobs)


if __name__ == "__main__":
    main()
