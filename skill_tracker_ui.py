import hashlib
import json
import os
import queue
import re
import threading
import time
from collections import deque
import tkinter as tk
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import brentq

try:
    from profession_data import PROFESSIONS
except ImportError:
    # Helpful when testing beside an exported file named like profession_data(3).py.
    import importlib.util
    local_file = Path(__file__).with_name("profession_data(3).py")
    if not local_file.exists():
        raise
    spec = importlib.util.spec_from_file_location("profession_data_fallback", local_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    PROFESSIONS = module.PROFESSIONS

try:
    from weapon_data import WEAPONS
except ImportError:
    WEAPONS = {}

try:
    from mob_data import MOBS
except ImportError:
    MOBS = {}

try:
    from amplifier_data import AMPLIFIERS
except ImportError:
    AMPLIFIERS = {}

try:
    from attachment_data import ATTACHMENTS
except ImportError:
    ATTACHMENTS = {}


CURRENT_SKILLS_FILE = Path("current_skills.json")
TRACKER_STATE_FILE = Path("skill_tracker_state.json")
SESSIONS_FILE = Path("skill_tracker_sessions.json")
ANALYSIS_SESSIONS_FILE = Path("mob_analysis_sessions.json")
HUNTING_SETUPS_FILE = Path("hunting_setups.json")
LOOT_MARKUPS_FILE = Path("loot_markups.json")
MARKET_DATA_FILE = Path("market_data.json")
IGNORED_LOOT_ITEMS = ("Universal Ammo", "Nanocube")
# Some stackables do not print quantity in chat.log. Derive count from TT value.
STACKABLE_ITEM_PED_VALUE = {"Shrapnel": 0.0001}
LOOT_TRACKER_GRAPH_VERSION = "loot-item-mu-summary-v12"
LOOT_EVENT_CONTINUE_SECONDS = 8

# Entropia attributes contribute to professions at 20 times their displayed
# value. For example, 10 Psyche is treated as 200 profession skill points
# before the profession weight is applied.
PROFESSION_ATTRIBUTES_X20 = {
    "Agility",
    "Intelligence",
    "Psyche",
    "Stamina",
    "Strength",
}


X = np.array([
    0, 100, 200, 300, 400, 500, 600, 700, 800, 900,
    1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900,
    2000, 2100, 2200, 2300, 2400, 2500, 2750, 3000, 3250, 3500,
    3750, 4000, 4250, 4500, 4750, 5000, 5250, 5500, 5750, 6000,
    6250, 6500, 6750, 7000, 7250, 7500, 7750, 8000, 8250, 8500,
    8750, 9000, 9250, 9500, 9750, 10000, 10250, 10500, 10750, 11000,
    11250, 11500, 11750, 12000, 12250, 12500, 12750, 13000, 13250, 13500,
    13750, 14000, 14250, 14500, 14750
], dtype=float)

Y = np.array([
    0, 0.12, 0.25, 0.42, 0.68, 1.08, 1.5, 1.83, 2.12, 2.51,
    3.09, 3.72, 4.2, 4.64, 5.21, 6.07, 6.99, 7.71, 8.35, 9.21,
    10.48, 11.85, 12.92, 13.87, 15.13, 17.02, 21.31, 26.69, 33.07, 41.03,
    50.51, 61.96, 75.71, 92.18, 111.9, 135.46, 163.58, 197.06, 236.93, 280.42,
    334.76, 396.85, 467.43, 547.32, 637.31, 738.28, 873.75, 1004.9, 1142.96, 1289.66,
    1442.97, 1604.53, 1772.41, 1948.16, 2129.93, 2319.2, 2514.19, 2716.29, 2923.82, 3138.08,
    3357.48, 3583.23, 3813.81, 4050.37, 4291.47, 4538.16, 4789.1, 5045.25, 5305.34, 5570.27,
    5838.85, 6111.88, 6388.27, 6668.74, 6952.25
], dtype=float)

Z = np.log(Y + 1.0)
_log_interpolator = PchipInterpolator(X, Z)


def skill_tt_value(skill_points: float) -> float:
    z = _log_interpolator(float(skill_points))
    return float(np.exp(z) - 1.0)


def find_skill_after_tt_delta(current_points: float, delta_tt: float) -> float:
    current_points = float(current_points)
    delta_tt = float(delta_tt)
    if delta_tt == 0:
        return current_points

    target_y = skill_tt_value(current_points) + delta_tt
    min_y = skill_tt_value(X[0])
    max_y = skill_tt_value(X[-1])

    if target_y < min_y or target_y > max_y:
        raise ValueError(
            f"Target TT value {target_y:.6f} is outside supported range "
            f"[{min_y:.6f}, {max_y:.6f}]"
        )

    def equation(x):
        return skill_tt_value(x) - target_y

    return float(brentq(equation, X[0], X[-1]))


def load_json(path: Path, default):
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return default


def save_json(path: Path, data):
    """Atomically save JSON and keep a backup of the previous valid file.

    A power loss during write should not leave the main JSON half-written.
    The temporary file is fully written/flushed and then moved into place with
    os.replace, which is atomic on the same filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=4, ensure_ascii=False)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    backup_path = path.with_suffix(path.suffix + ".bak")
    backup_temp_path = path.with_suffix(path.suffix + ".bak.tmp")

    if path.exists():
        try:
            backup_temp_path.write_bytes(path.read_bytes())
            os.replace(backup_temp_path, backup_path)
        except Exception:
            try:
                if backup_temp_path.exists():
                    backup_temp_path.unlink()
            except Exception:
                pass

    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)




def load_weekly_market_markups(path: Path):
    """Load weekly markup from the single latest record stored for each item."""
    data = load_json(path, {})
    result = {}
    if not isinstance(data, dict):
        return result

    items = data.get("items", {})
    if not isinstance(items, dict):
        return result

    for market_entry in items.values():
        if not isinstance(market_entry, dict):
            continue
        item_name = str(market_entry.get("item", "") or "").strip()
        if not item_name:
            continue

        markup_type = None
        markup_value = None
        periods = market_entry.get("periods", {})
        if isinstance(periods, dict):
            week = periods.get("week", {})
            if isinstance(week, dict):
                markup = week.get("markup", {})
                if isinstance(markup, dict):
                    markup_type = markup.get("type")
                    markup_value = markup.get("value")

        try:
            markup_value = float(markup_value)
        except (TypeError, ValueError):
            continue

        if markup_type == "percentage" and markup_value >= 0:
            result[item_name] = {"type": "percentage", "value": markup_value}
        elif markup_type == "fixed" and markup_value >= 0:
            result[item_name] = {"type": "fixed", "value": markup_value}

    return result


def load_current_skills():
    data = load_json(CURRENT_SKILLS_FILE, {})
    result = {}
    for key, value in data.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            pass
    return result


def save_current_skills(skills):
    save_json(CURRENT_SKILLS_FILE, {k: float(v) for k, v in sorted(skills.items())})



def log_resume_fingerprint(path: Path, offset: int, window: int = 8192) -> str:
    """Return a small fingerprint of bytes immediately before offset.

    This lets the tracker safely resume only when the file content before the
    saved offset is still the same. If chat.log was cleared, overwritten,
    rotated, or replaced by another log with the same path, the fingerprint will
    not match and the tracker will restart from byte 0 instead of skipping or
    corrupting data.
    """
    try:
        offset = int(offset)
        if offset <= 0 or not path.exists():
            return ""
        size = path.stat().st_size
        if offset > size:
            return ""
        start = max(0, offset - window)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(offset - start)
        digest = hashlib.sha256(data).hexdigest()
        return f"{start}:{offset}:{digest}"
    except Exception:
        return ""


def can_resume_log(path: Path, saved_path: str, saved_offset: int, saved_fingerprint: str) -> bool:
    try:
        saved_offset = int(saved_offset)
    except (TypeError, ValueError):
        return False

    if str(path) != str(saved_path or ""):
        return False
    if not path.exists():
        return False
    current_size = path.stat().st_size
    if saved_offset < 0 or saved_offset > current_size:
        return False
    if saved_offset == 0:
        return True
    if not saved_fingerprint:
        return False
    return log_resume_fingerprint(path, saved_offset) == saved_fingerprint


def newest_log_timestamp_at_or_before(path: Path, end_offset: int, max_scan: int = 8 * 1024 * 1024):
    """Return the newest timestamped chat.log line at or before end_offset."""
    try:
        end_offset = int(end_offset)
        if end_offset <= 0 or not path.exists():
            return None
        file_size = path.stat().st_size
        pos = min(end_offset, file_size)
        data = b""
        scanned = 0
        with path.open("rb") as handle:
            while pos > 0 and scanned < max_scan:
                read_size = min(65536, pos, max_scan - scanned)
                if read_size <= 0:
                    break
                pos -= read_size
                handle.seek(pos)
                data = handle.read(read_size) + data
                scanned += read_size
                for raw_line in reversed(data.splitlines()):
                    line = raw_line.decode("utf-8", errors="replace")
                    timestamp = ChatLogParser.parse_line_timestamp(line)
                    if timestamp is not None:
                        return timestamp
    except Exception:
        return None
    return None


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def parse_chat_timestamp(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_any_timestamp(value):
    return parse_chat_timestamp(value) or parse_iso_datetime(value)


def event_is_after_cutoff(event, cutoff_at):
    """Return True when an event should be processed after a safe-reset read.

    chat.log timestamps have only second precision, while last_log_read_at is an
    app timestamp. Using >= avoids losing a real new event that happened during
    the same second as the previous state save.
    """
    if cutoff_at is None:
        return True
    event_at = parse_chat_timestamp((event or {}).get("timestamp"))
    if event_at is None:
        return False
    return event_at >= cutoff_at


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def percent(numerator, denominator) -> float:
    try:
        denominator = float(denominator)
        if denominator == 0:
            return 0.0
        return float(numerator) / denominator * 100.0
    except (TypeError, ValueError):
        return 0.0




def profession_weighted_value(skill_name: str, value: float) -> float:
    """Return the value used by every profession formula.

    Attributes are displayed on a 0-100 style scale in Entropia but count as
    twenty skill points per displayed point in profession calculations.
    Regular skills are used unchanged.
    """
    value = parse_float(value, 0.0)
    if str(skill_name).strip() in PROFESSION_ATTRIBUTES_X20:
        return value * 20.0
    return value

def ignored_loot_item_name(item_name: str) -> bool:
    lower_name = str(item_name or "").lower()
    return any(item.lower() == lower_name for item in IGNORED_LOOT_ITEMS)


def ignored_loot_message(message: str) -> bool:
    lower_message = str(message or "").lower()
    return any(item.lower() in lower_message for item in IGNORED_LOOT_ITEMS)


def normalize_loot_item_name(raw_name: str) -> str:
    """Return a stable item name from the text between 'You received' and 'Value:'.

    Entropia loot messages often include quantities, for example
    'Animal Muscle Oil x 12'.  For item count charts we want the item name,
    not a separate bucket for every quantity.
    """
    name = str(raw_name or "").strip()
    name = re.sub(r"\s+x\s*\(?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*\)?$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+\([0-9][0-9,]*(?:\.[0-9]+)?\)$", "", name).strip()
    return name or "Unknown item"


def parse_loot_item_quantity(raw_name: str, item_name: str | None = None, value_ped: float | None = None) -> int:
    name = str(raw_name or "")
    match = re.search(r"\s+x\s*\(?\s*([0-9][0-9,]*)\s*\)?", name, flags=re.IGNORECASE)
    if match:
        try:
            return max(1, int(match.group(1).replace(",", "")))
        except ValueError:
            return 1

    # Also accept old/simple quantity suffixes like "Animal Hide (12)".
    match = re.search(r"\(([0-9][0-9,]*)\)\s*$", name)
    if match:
        try:
            return max(1, int(match.group(1).replace(",", "")))
        except ValueError:
            return 1

    normalized = item_name or normalize_loot_item_name(raw_name)
    unit_value = STACKABLE_ITEM_PED_VALUE.get(normalized)
    if unit_value and value_ped is not None:
        try:
            return max(1, int(round(float(value_ped) / float(unit_value))))
        except (TypeError, ValueError, ZeroDivisionError):
            return 1
    return 1


def equipment_item_cost_per_shot_ped(item: dict) -> float:
    decay = item.get("decay") or 0.0
    ammo_burn = item.get("ammo_burn") or 0.0
    # Entropia data is usually in PEC-like fractional decay and ammo burn in ammo units.
    # PED cost = decay PEC / 100 + ammo units / 10000.
    return float(decay) / 100.0 + float(ammo_burn) / 10000.0


def equipment_item_max_damage(item: dict) -> float:
    return parse_float(item.get("max_damage"), 0.0)


def weapon_cost_per_shot_ped(weapon_name: str) -> float:
    return equipment_item_cost_per_shot_ped(WEAPONS.get(weapon_name) or {})


def hunting_setup_cost_per_shot_ped(weapon_name: str, amplifier_name: str = "", attachment_names=None) -> float:
    cost = weapon_cost_per_shot_ped(weapon_name)
    if amplifier_name in AMPLIFIERS:
        cost += equipment_item_cost_per_shot_ped(AMPLIFIERS[amplifier_name])
    for attachment_name in list(attachment_names or [])[:3]:
        if attachment_name in ATTACHMENTS:
            cost += equipment_item_cost_per_shot_ped(ATTACHMENTS[attachment_name])
    return cost


def hunting_setup_max_damage(weapon_name: str, amplifier_name: str = "") -> float:
    max_damage = equipment_item_max_damage(WEAPONS.get(weapon_name) or {})
    if amplifier_name in AMPLIFIERS:
        max_damage += equipment_item_max_damage(AMPLIFIERS[amplifier_name])
    return max_damage


def hunting_setup_efficiency(weapon_name: str) -> float | None:
    weapon = WEAPONS.get(weapon_name) or {}
    efficiency = parse_float(weapon.get("efficiency"), None)
    return efficiency


def hunting_setup_uses_per_minute(weapon_name: str) -> float:
    weapon = WEAPONS.get(weapon_name) or {}
    return parse_float(weapon.get("uses_per_minute"), 0.0)


def hunting_setup_dpp(weapon_name: str, amplifier_name: str = "", attachment_names=None) -> float:
    cost_ped = hunting_setup_cost_per_shot_ped(weapon_name, amplifier_name, attachment_names)
    cost_pec = cost_ped * 100.0
    if cost_pec == 0:
        return 0.0
    return (0.695 * hunting_setup_max_damage(weapon_name, amplifier_name)) / cost_pec


def hunting_setup_ped_per_hour(weapon_name: str, amplifier_name: str = "", attachment_names=None) -> float:
    cost_ped = hunting_setup_cost_per_shot_ped(weapon_name, amplifier_name, attachment_names)
    return cost_ped * hunting_setup_uses_per_minute(weapon_name) * 60.0


def avg_ped_loss_per_100(efficiency: float | None) -> float | None:
    if efficiency is None:
        return None
    return (0.07 - (0.0007 * efficiency)) * 100.0


@dataclass
class MonitorSession:
    id: str
    started_at: str
    ended_at: str | None = None
    chat_log_path: str = ""
    start_offset: int = 0
    end_offset: int = 0
    log_cutoff_at: str = ""
    weapon: str = ""
    amplifier: str = ""
    attachments: list = field(default_factory=list)
    mob: str = ""
    maturity: str = ""
    count_hunting: bool = False
    current_skills_at_start: dict = field(default_factory=dict)
    current_skills_at_end: dict = field(default_factory=dict)
    # Skill gain details saved with every session.
    # IMPORTANT: Entropia chat.log gain values are skill-point deltas, not TT deltas.
    # skill_gains_points: sum of raw chat.log gains per skill.
    # skill_gains_tt: derived TT-equivalent gain from old/new skill-point values.
    # skill_gain_events_by_skill: how many separate gain messages were seen per skill.
    # skill_gain_tt_total / skill_gain_points_total: totals across all skills.
    skill_gains_tt: dict = field(default_factory=dict)
    skill_gains_points: dict = field(default_factory=dict)
    skill_gain_events_by_skill: dict = field(default_factory=dict)
    skill_gain_tt_total: float = 0.0
    skill_gain_points_total: float = 0.0
    skill_gain_mode: str = "chat_points_plus_tt_equivalent"
    total_profession_gain_by_profession: dict = field(default_factory=dict)
    normal_hits: int = 0
    critical_hits: int = 0
    # Target defenses are grouped together because the exact message depends
    # on weapon type: Jammed, Evaded, or Dodged.
    defended_attacks: int = 0
    # A plain "You missed" is kept separate because it is caused by hit ability.
    missed_attacks: int = 0
    attacks_total: int = 0
    damage_total: float = 0.0
    loot_ped_total: float = 0.0
    loot_event_count: int = 0
    loot_events: list = field(default_factory=list)
    ped_cycled: float = 0.0
    events: list = field(default_factory=list)


class ChatLogParser:
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^]]*)\] \[([^]]*)\] (.*)$")
    gain_direct_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) ([A-Za-z][A-Za-z '\-]+)$")
    gain_experience_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) experience in your (.+?) skill$")
    improved_re = re.compile(r"^Your (.+?) has improved by ([0-9]+(?:\.[0-9]+)?)$")
    normal_damage_re = re.compile(r"^You inflicted ([0-9]+(?:\.[0-9]+)?) points of damage$")
    crit_damage_re = re.compile(r"^Critical hit - Additional damage! You inflicted ([0-9]+(?:\.[0-9]+)?) points of damage$")
    target_defense_re = re.compile(r"^The target (Jammed|Evaded|Dodged) your attack$")
    loot_re = re.compile(r"^You received (.+?) Value: ([0-9]+(?:\.[0-9]+)?) PED$")

    @classmethod
    def parse_line_timestamp(cls, line: str):
        match = cls.line_re.match(line.strip("\ufeff\r\n"))
        if not match:
            return None
        return parse_chat_timestamp(match.group(1))

    @classmethod
    def parse_line(cls, line: str):
        line = line.strip("\ufeff\r\n")
        match = cls.line_re.match(line)
        if not match:
            return None

        timestamp, channel, sender, message = match.groups()
        if channel != "System":
            return None

        crit = cls.crit_damage_re.match(message)
        if crit:
            return {"type": "crit", "timestamp": timestamp, "damage": float(crit.group(1)), "message": message}

        normal = cls.normal_damage_re.match(message)
        if normal:
            return {"type": "normal_hit", "timestamp": timestamp, "damage": float(normal.group(1)), "message": message}

        target_defense = cls.target_defense_re.match(message)
        if target_defense:
            return {
                "type": "defended_attack",
                "timestamp": timestamp,
                "defense": target_defense.group(1).lower(),
                "message": message,
            }

        if message == "You missed":
            return {"type": "miss", "timestamp": timestamp, "message": message}

        gain = cls.gain_experience_re.match(message)
        if gain:
            return {
                "type": "skill_gain",
                "timestamp": timestamp,
                "skill": gain.group(2).strip(),
                "delta_tt": float(gain.group(1)),
                "message": message,
            }

        gain = cls.gain_direct_re.match(message)
        if gain:
            return {
                "type": "skill_gain",
                "timestamp": timestamp,
                "skill": gain.group(2).strip(),
                "delta_tt": float(gain.group(1)),
                "message": message,
            }

        improved = cls.improved_re.match(message)
        if improved:
            return {
                "type": "skill_gain",
                "timestamp": timestamp,
                "skill": improved.group(1).strip(),
                "delta_tt": float(improved.group(2)),
                "message": message,
            }

        loot = cls.loot_re.match(message)
        if loot:
            if ignored_loot_message(message):
                return None
            raw_item_name = loot.group(1).strip()
            item_name = normalize_loot_item_name(raw_item_name)
            value_ped = float(loot.group(2))
            return {
                "type": "loot",
                "timestamp": timestamp,
                "item": item_name,
                "quantity": parse_loot_item_quantity(raw_item_name, item_name, value_ped),
                "value_ped": value_ped,
                "message": message,
            }

        return None


class SkillTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Entropia Skill Tracker")
        self.root.geometry("1280x820")

        self.current_skills = load_current_skills()
        self.state = load_json(TRACKER_STATE_FILE, {})
        self.sessions = load_json(SESSIONS_FILE, [])
        self.analysis_sessions = load_json(ANALYSIS_SESSIONS_FILE, [])
        if not isinstance(self.analysis_sessions, list):
            self.analysis_sessions = []
        self.hunting_setups = load_json(HUNTING_SETUPS_FILE, {})
        if not isinstance(self.hunting_setups, dict):
            self.hunting_setups = {}
        raw_loot_markups = load_json(LOOT_MARKUPS_FILE, {})
        self.loot_markups = {}
        self.market_weekly_markups = load_weekly_market_markups(MARKET_DATA_FILE)
        try:
            self.market_data_mtime_ns = MARKET_DATA_FILE.stat().st_mtime_ns
        except OSError:
            self.market_data_mtime_ns = 0
        if isinstance(raw_loot_markups, dict):
            for item_name, markup in raw_loot_markups.items():
                try:
                    markup_value = float(markup)
                except (TypeError, ValueError):
                    continue
                if markup_value >= 100.0:
                    self.loot_markups[str(item_name)] = markup_value

        self.current_session: MonitorSession | None = None
        self.monitoring = False
        self.sync_paused = False
        self.log_offset = int(self.state.get("last_log_offset", 0) or 0)
        self.last_log_path = self.state.get("chat_log_path", "")
        self.log_time_cutoff_at = None

        # Background log reader state. Large chat.log files are parsed in a
        # worker thread, and the Tkinter UI applies parsed events in small
        # batches so the window does not freeze.
        self.reader_thread = None
        self.reader_queue = queue.Queue()
        self.reader_stop_event = threading.Event()
        self.reader_active = False
        self.reader_done_pending = False
        self.reader_final_offset = None
        self.reader_final_last_read_at = ""
        self.reader_error = ""
        self.reader_last_progress_at = 0.0
        self.reader_processed_batches = 0

        # Keep log parsing and the Tkinter UI decoupled. Parsed data is applied
        # immediately, while expensive widget redraws and disk writes are
        # throttled. This prevents long chat.log imports from making the window
        # appear frozen even though the reader is still progressing.
        self.live_ui_dirty = False
        self.live_ui_last_refresh_at = 0.0
        self.live_ui_refresh_interval = 0.45
        self.live_persist_last_at = 0.0
        self.live_persist_interval = 1.5
        self.pending_last_log_read_at = ""
        self.pending_event_lines = deque(maxlen=1200)
        self.recent_event_line_limit = 600
        self.last_loot_live_refresh_at = 0.0
        self.loot_live_refresh_interval = 3.0

        self.profession_var = tk.StringVar()
        default_projection_profession = "Animal Looter" if "Animal Looter" in PROFESSIONS else next(iter(PROFESSIONS), "")
        self.session_projection_profession_var = tk.StringVar(value=default_projection_profession)
        self.session_projection_ped_var = tk.StringVar(value="1000")
        self.total_gain_var = tk.StringVar(value="Total profession gain: 0.0000")
        self.selected_skill_var = tk.StringVar(value="")
        self.current_var = tk.StringVar(value="0")
        self.delta_var = tk.StringVar(value="0")
        self.auto_update_current_skills_var = tk.BooleanVar(value=True)
        self.entries = {}

        self.chat_log_path_var = tk.StringVar(value=self.last_log_path)
        self.monitor_status_var = tk.StringVar(value="Stopped")
        self.monitor_progress_var = tk.StringVar(value="")
        self.session_summary_var = tk.StringVar(value="No active session")
        self.sync_start_modes = (
            "Resume saved position",
            "From chosen time",
            "From start of log",
            "From end of log",
        )
        self.sync_start_mode_var = tk.StringVar(value=self.state.get("sync_start_mode", self.sync_start_modes[0]))
        if self.sync_start_mode_var.get() not in self.sync_start_modes:
            self.sync_start_mode_var.set(self.sync_start_modes[0])
        self.last_log_read_at_var = tk.StringVar(value=self.state.get("last_log_read_at", ""))

        self.weapon_var = tk.StringVar(value=self.state.get("weapon", ""))
        self.amplifier_var = tk.StringVar(value=self.state.get("amplifier", ""))
        saved_attachments = list(self.state.get("attachments", []) or [])[:3]
        while len(saved_attachments) < 3:
            saved_attachments.append("")
        self.attachment_vars = [tk.StringVar(value=value) for value in saved_attachments]
        self.mob_var = tk.StringVar(value=self.state.get("mob", ""))
        self.maturity_var = tk.StringVar(value=self.state.get("maturity", ""))
        self.count_hunting_var = tk.BooleanVar(value=bool(self.state.get("count_hunting", False)))
        self.hunting_setup_name_var = tk.StringVar(value=str(self.state.get("selected_hunting_setup", "") or ""))
        self.hunting_setup_status_var = tk.StringVar(value="")
        self.weapon_cost_var = tk.StringVar(value="Cost/shot: 0.000000 PED")
        self.mob_info_var = tk.StringVar(value="Mob: -")
        self.all_weapon_names = sorted(WEAPONS.keys(), key=str.lower)
        self.all_amplifier_names = sorted(AMPLIFIERS.keys(), key=str.lower)
        self.all_attachment_names = sorted(ATTACHMENTS.keys(), key=str.lower)
        self.all_mob_names = sorted(MOBS.keys(), key=str.lower)
        self.weapon_filter_var = tk.StringVar()
        self.amplifier_filter_var = tk.StringVar()
        self.attachment_filter_var = tk.StringVar()
        self.mob_filter_var = tk.StringVar()
        self.tree_sort_state = {}
        self.tree_heading_titles = {}
        self.loot_summary_var = tk.StringVar(value="No loot session selected")
        self.loot_markup_status_var = tk.StringVar(value="Double-click an item row for its drop history; double-click the MU cell to set a manual percentage. Otherwise weekly market_data.json markup is used, then 100%.")
        self.loot_item_summary_iid_to_name = {}
        self.loot_markup_editor = None
        self.loot_event_iid_to_event = {}
        self.loot_item_filter_var = tk.StringVar()
        self.loot_item_vars = {}
        self.loot_chart_payloads = {}
        self.loot_chart_meta = {}
        self.loot_zoom_ranges = {}
        self.loot_drag = None
        self.loot_selection_var = tk.StringVar(value="Drag across any loot graph to zoom and show totals for the selected range.")
        self.loot_item_check_signature = None
        self.loot_refresh_after_id = None

        # Mob-hunting analysis inputs. Each mob uses the looter profession that
        # matches MOBS[mob]["type"]. Only explicitly exported valid sessions
        # from ANALYSIS_SESSIONS_FILE participate in these calculations.
        self.analysis_efficiency_var = tk.StringVar(value=str(self.state.get("analysis_efficiency", "0")))
        # Looter levels are calculated from current_skills.json on startup.
        # The Entry widgets remain editable, so calculated values can be
        # overridden manually for hypothetical analysis.
        self.analysis_looter_vars = {
            "Animal": tk.StringVar(value="0"),
            "Robot": tk.StringVar(value="0"),
            "Mutant": tk.StringVar(value="0"),
        }
        self.load_analysis_looters_from_current_skills()
        self.mob_analysis_status_var = tk.StringVar(value="No analysis calculated yet.")
        self.mob_analysis_iid_to_result = {}
        self.mob_analysis_results = []

        self.create_ui()
        self.load_profession()
        self.refresh_hunting_info()
        self.refresh_sessions_table()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(500, self.monitor_tick)

    def create_ui(self):
        notebook = ttk.Notebook(self.root)
        self.notebook = notebook
        notebook.pack(fill="both", expand=True)

        self.profession_tab = ttk.Frame(notebook)
        self.monitor_tab = ttk.Frame(notebook)
        self.hunting_tab = ttk.Frame(notebook)
        self.sessions_tab = ttk.Frame(notebook)
        self.session_details_tab = ttk.Frame(notebook)
        self.loot_tab = ttk.Frame(notebook)
        self.mob_analysis_tab = ttk.Frame(notebook)

        notebook.add(self.monitor_tab, text="Live Monitor")
        notebook.add(self.loot_tab, text="Loot Tracker")
        notebook.add(self.hunting_tab, text="Hunting Setup")
        notebook.add(self.sessions_tab, text="Previous Sessions")
        notebook.add(self.mob_analysis_tab, text="What Mob to Hunt")
        notebook.add(self.session_details_tab, text="Session Details")
        notebook.add(self.profession_tab, text="Professions / Skills")

        self.create_monitor_tab()
        self.create_loot_tab()
        self.create_hunting_tab()
        self.create_sessions_tab()
        self.create_mob_analysis_tab()
        self.create_session_details_tab()
        self.create_profession_tab()
        notebook.bind("<<NotebookTabChanged>>", self.on_notebook_tab_changed)

    def is_tab_active(self, tab):
        try:
            return self.notebook.select() == str(tab)
        except (AttributeError, tk.TclError):
            return False

    def on_notebook_tab_changed(self, event=None):
        # Heavy loot tables/charts are refreshed on demand when their tab is
        # opened, rather than after every parsed log batch.
        if self.is_tab_active(getattr(self, "loot_tab", None)):
            self.refresh_loot_tab()
            self.last_loot_live_refresh_at = time.monotonic()
        elif self.is_tab_active(getattr(self, "profession_tab", None)):
            self.refresh_profession_current_values()
        elif self.is_tab_active(getattr(self, "mob_analysis_tab", None)):
            self.refresh_mob_analysis()

    def make_tree_sortable(self, tree, headings):
        self.tree_heading_titles[tree] = dict(headings)
        self.tree_sort_state.setdefault(tree, {"column": None, "descending": False})
        self.refresh_tree_sort_headings(tree)

    def refresh_tree_sort_headings(self, tree):
        titles = self.tree_heading_titles.get(tree, {})
        state = self.tree_sort_state.get(tree, {})
        active_column = state.get("column")
        descending = bool(state.get("descending", False))
        for col, title in titles.items():
            indicator = ""
            if col == active_column:
                indicator = " v" if descending else " ^"
            tree.heading(col, text=f"{title}{indicator}", command=lambda c=col, t=tree: self.toggle_tree_sort(t, c))

    def toggle_tree_sort(self, tree, column):
        state = self.tree_sort_state.setdefault(tree, {"column": None, "descending": False})
        if state.get("column") == column:
            state["descending"] = not bool(state.get("descending", False))
        else:
            state["column"] = column
            state["descending"] = False
        self.apply_tree_sort(tree)
        self.refresh_tree_sort_headings(tree)

    def tree_sort_key(self, value):
        text = str(value or "").strip()
        if not text:
            return None
        numeric_text = text.replace(",", "").strip()
        # Numeric table cells may be formatted as 84.20%, 1.23x, or 0.0123 PED.
        numeric_text = re.sub(r"\s*(%|x|PED)\s*$", "", numeric_text, flags=re.IGNORECASE)
        try:
            return (0, float(numeric_text))
        except ValueError:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", numeric_text)
            if match:
                try:
                    return (0, float(match.group(0)))
                except ValueError:
                    pass
            return (1, text.casefold())

    def apply_tree_sort(self, tree):
        state = self.tree_sort_state.get(tree, {})
        column = state.get("column")
        if not column:
            return
        descending = bool(state.get("descending", False))
        keyed_items = []
        empty_items = []
        for item_id in tree.get_children(""):
            key = self.tree_sort_key(tree.set(item_id, column))
            if key is None:
                empty_items.append(item_id)
            else:
                keyed_items.append((key, item_id))
        keyed_items.sort(key=lambda item: item[0], reverse=descending)
        ordered_items = [item_id for _, item_id in keyed_items] + empty_items
        for index, item_id in enumerate(ordered_items):
            tree.move(item_id, "", index)

    def create_profession_tab(self):
        top_frame = ttk.Frame(self.profession_tab, padding=10)
        top_frame.pack(fill="x")

        ttk.Label(top_frame, text="Profession:").pack(side="left")
        self.profession_combo = ttk.Combobox(
            top_frame,
            textvariable=self.profession_var,
            values=list(PROFESSIONS.keys()),
            width=45,
            state="readonly",
        )
        self.profession_combo.pack(side="left", padx=8)
        self.profession_var.set("Animal Looter" if "Animal Looter" in PROFESSIONS else list(PROFESSIONS.keys())[0])
        self.profession_combo.bind("<<ComboboxSelected>>", lambda e: self.load_profession())

        ttk.Button(top_frame, text="Calculate", command=self.calculate_profession_gain).pack(side="left", padx=5)
        ttk.Button(top_frame, text="Save Current Skills", command=self.save_current_skills_from_table).pack(side="left", padx=5)
        ttk.Button(top_frame, text="Reload Saved Skills", command=self.reload_saved_skills).pack(side="left", padx=5)
        ttk.Checkbutton(
            top_frame,
            text="After calculate, set current skills = new x2",
            variable=self.auto_update_current_skills_var,
        ).pack(side="left", padx=10)
        ttk.Label(top_frame, textvariable=self.total_gain_var, font=("Arial", 12, "bold")).pack(side="right", padx=10)

        columns = ("skill", "weight", "current", "delta", "new", "skill_gain", "profession_gain")
        self.skill_tree = ttk.Treeview(self.profession_tab, columns=columns, show="headings", height=24)
        headings = {
            "skill": "Skill", "weight": "Weight %", "current": "Current x1", "delta": "TT delta",
            "new": "New x2", "skill_gain": "Skill gain", "profession_gain": "Profession gain"
        }
        widths = {"skill": 250, "weight": 90, "current": 130, "delta": 120, "new": 130, "skill_gain": 130, "profession_gain": 150}
        for col in columns:
            self.skill_tree.heading(col, text=headings[col])
            self.skill_tree.column(col, width=widths[col], anchor="center" if col != "skill" else "w")
        self.skill_tree.pack(fill="both", expand=True, padx=10, pady=10)

        input_frame = ttk.LabelFrame(self.profession_tab, text="Edit selected skill", padding=10)
        input_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(input_frame, text="Selected skill:").grid(row=0, column=0, sticky="w")
        ttk.Label(input_frame, textvariable=self.selected_skill_var, width=28).grid(row=0, column=1, sticky="w")
        ttk.Label(input_frame, text="Current x1:").grid(row=0, column=2, sticky="w", padx=(20, 4))
        ttk.Entry(input_frame, textvariable=self.current_var, width=15).grid(row=0, column=3, sticky="w")
        ttk.Label(input_frame, text="TT delta:").grid(row=0, column=4, sticky="w", padx=(20, 4))
        ttk.Entry(input_frame, textvariable=self.delta_var, width=15).grid(row=0, column=5, sticky="w")
        ttk.Button(input_frame, text="Apply to selected skill", command=self.apply_selected_skill).grid(row=0, column=6, padx=20)
        ttk.Button(input_frame, text="Save selected current skill", command=self.save_selected_current_skill).grid(row=0, column=7, padx=5)
        self.skill_tree.bind("<<TreeviewSelect>>", self.on_skill_selected)

    def create_monitor_tab(self):
        top = ttk.Frame(self.monitor_tab, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="chat.log:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.chat_log_path_var, width=90).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self.browse_chat_log).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="Start Sync", command=self.start_sync).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Stop Sync", command=self.stop_sync).grid(row=0, column=4, padx=4)
        self.pause_sync_button = ttk.Button(top, text="Pause Sync", command=self.toggle_pause_sync)
        self.pause_sync_button.grid(row=0, column=5, padx=4)
        ttk.Label(top, textvariable=self.monitor_status_var, font=("Arial", 11, "bold")).grid(row=0, column=6, padx=12)
        ttk.Label(top, text="Start from:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(
            top,
            textvariable=self.sync_start_mode_var,
            values=self.sync_start_modes,
            width=24,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(top, text="last_log_read_at:").grid(row=1, column=2, sticky="e", padx=(8, 4), pady=(6, 0))
        ttk.Entry(top, textvariable=self.last_log_read_at_var, width=25).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Button(top, text="Save Time", command=self.save_last_log_read_at_from_ui).grid(row=1, column=4, sticky="w", padx=4, pady=(6, 0))
        ttk.Button(top, text="Clear Time", command=self.clear_last_log_read_at).grid(row=1, column=5, sticky="w", padx=4, pady=(6, 0))
        ttk.Button(top, text="Reload Time", command=self.reload_last_log_read_at).grid(row=1, column=6, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(top, textvariable=self.monitor_progress_var).grid(row=2, column=1, columnspan=6, sticky="w", padx=6, pady=(4, 0))
        top.columnconfigure(1, weight=1)

        summary = ttk.LabelFrame(self.monitor_tab, text="Current session", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.session_summary_var, justify="left").pack(anchor="w")

        columns = ("skill", "tt_gain", "tt_percent", "point_gain", "gain_count", "message_percent", "current")
        self.session_skill_tree = ttk.Treeview(self.monitor_tab, columns=columns, show="headings", height=10)
        for col, title, width in [
            ("skill", "Skill", 280), ("tt_gain", "TT-equivalent gain", 160),
            ("tt_percent", "TT % of skills", 120),
            ("point_gain", "Session point gain", 170), ("gain_count", "Gain messages", 130),
            ("message_percent", "Msg % of skills", 120),
            ("current", "Current skill", 160),
        ]:
            self.session_skill_tree.heading(col, text=title)
            self.session_skill_tree.column(col, width=width, anchor="center" if col != "skill" else "w")
        self.make_tree_sortable(self.session_skill_tree, {
            "skill": "Skill",
            "tt_gain": "TT-equivalent gain",
            "tt_percent": "TT % of skills",
            "point_gain": "Session point gain",
            "gain_count": "Gain messages",
            "message_percent": "Msg % of skills",
            "current": "Current skill",
        })
        self.session_skill_tree.pack(fill="both", expand=True, padx=10, pady=6)
        self.session_skill_tree.bind(
            "<Double-1>",
            lambda event: self.open_skill_gain_details_from_tree(event, self.session_skill_tree, "active"),
        )

        event_frame = ttk.LabelFrame(self.monitor_tab, text="Recent parsed events", padding=6)
        event_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.event_text = tk.Text(event_frame, height=10, wrap="none")
        self.event_text.pack(fill="both", expand=True)


    def create_loot_tab(self):
        top = ttk.Frame(self.loot_tab, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top,
            text=(
                "Uses the active session while syncing; otherwise uses the selected previous session, or the latest saved session. "
                f"Graph version: {LOOT_TRACKER_GRAPH_VERSION}"
            ),
        ).pack(side="left")
        ttk.Button(top, text="Refresh", command=self.refresh_loot_tab).pack(side="right", padx=4)
        ttk.Button(top, text="Reset zoom", command=self.reset_loot_zoom).pack(side="right", padx=4)

        summary = ttk.LabelFrame(self.loot_tab, text="Loot summary", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.loot_summary_var, justify="left").pack(anchor="w")

        item_summary_frame = ttk.LabelFrame(self.loot_tab, text="Looted items grouped by name", padding=6)
        item_summary_frame.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(item_summary_frame, textvariable=self.loot_markup_status_var).pack(anchor="w", pady=(0, 4))
        item_columns = ("item", "quantity", "value", "loot_percent", "markup", "after_mu")
        self.loot_item_summary_tree = ttk.Treeview(
            item_summary_frame,
            columns=item_columns,
            show="headings",
            height=7,
        )
        item_setup = [
            ("item", "Item", 300),
            ("quantity", "Quantity", 90),
            ("value", "Value PED", 105),
            ("loot_percent", "% of total loot", 115),
            ("markup", "MU", 110),
            ("after_mu", "Value after MU", 125),
        ]
        for col, title, width in item_setup:
            self.loot_item_summary_tree.heading(col, text=title)
            self.loot_item_summary_tree.column(col, width=width, anchor="w" if col == "item" else "center")
        self.make_tree_sortable(self.loot_item_summary_tree, {col: title for col, title, _ in item_setup})
        item_summary_scroll = ttk.Scrollbar(item_summary_frame, orient="vertical", command=self.loot_item_summary_tree.yview)
        self.loot_item_summary_tree.configure(yscrollcommand=item_summary_scroll.set)
        self.loot_item_summary_tree.pack(side="left", fill="x", expand=True)
        item_summary_scroll.pack(side="right", fill="y")
        self.loot_item_summary_tree.bind("<Double-1>", self.on_loot_item_summary_double_click)

        selection = ttk.LabelFrame(self.loot_tab, text="Graph selection / zoom", padding=8)
        selection.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(selection, textvariable=self.loot_selection_var, justify="left").pack(anchor="w")

        body = ttk.Panedwindow(self.loot_tab, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)

        items_frame = ttk.LabelFrame(left, text="Item filter", padding=6)
        items_frame.pack(fill="both", expand=True)
        ttk.Label(items_frame, text="Filter item names:").pack(anchor="w")
        item_filter = ttk.Entry(items_frame, textvariable=self.loot_item_filter_var)
        item_filter.pack(fill="x", pady=(2, 6))
        item_filter.bind("<KeyRelease>", lambda event: self.refresh_loot_item_checks(force=True))
        ttk.Button(items_frame, text="Apply selected items", command=self.refresh_loot_tab).pack(fill="x", pady=(0, 4))
        ttk.Button(items_frame, text="Clear item selection", command=self.clear_loot_item_selection).pack(fill="x", pady=(0, 8))

        canvas_holder = ttk.Frame(items_frame)
        canvas_holder.pack(fill="both", expand=True)
        self.loot_items_canvas = tk.Canvas(canvas_holder, highlightthickness=0, width=260)
        self.loot_items_scrollbar = ttk.Scrollbar(canvas_holder, orient="vertical", command=self.loot_items_canvas.yview)
        self.loot_items_scrollable = ttk.Frame(self.loot_items_canvas)
        self.loot_items_scrollable.bind(
            "<Configure>",
            lambda event: self.loot_items_canvas.configure(scrollregion=self.loot_items_canvas.bbox("all")),
        )
        self.loot_items_canvas.create_window((0, 0), window=self.loot_items_scrollable, anchor="nw")
        self.loot_items_canvas.configure(yscrollcommand=self.loot_items_scrollbar.set)
        self.loot_items_canvas.pack(side="left", fill="both", expand=True)
        self.loot_items_scrollbar.pack(side="right", fill="y")

        events_frame = ttk.LabelFrame(left, text="Loot events", padding=6)
        events_frame.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("idx", "time", "items", "loot", "cost", "return")
        self.loot_events_tree = ttk.Treeview(events_frame, columns=columns, show="headings", height=10)
        setup = [
            ("idx", "#", 45),
            ("time", "Time", 135),
            ("items", "Items", 65),
            ("loot", "Loot PED", 80),
            ("cost", "Cost PED", 80),
            ("return", "Multiplier", 85),
        ]
        for col, title, width in setup:
            self.loot_events_tree.heading(col, text=title)
            self.loot_events_tree.column(col, width=width, anchor="center")
        self.make_tree_sortable(self.loot_events_tree, {col: title for col, title, _ in setup})
        self.loot_events_tree.pack(fill="both", expand=True)
        self.loot_events_tree.bind("<Double-1>", self.open_loot_event_details)

        graph1 = ttk.LabelFrame(right, text="1. Loot value / elapsed time (% return)", padding=6)
        graph1.pack(fill="both", expand=True, pady=(0, 6))
        self.loot_value_time_canvas = tk.Canvas(graph1, height=210, background="#ffffff", highlightthickness=1, highlightbackground="#d7dce2")
        self.loot_value_time_canvas.pack(fill="both", expand=True)

        graph2 = ttk.LabelFrame(right, text="2. Loot value / cost per kill (PED, multiplier)", padding=6)
        graph2.pack(fill="both", expand=True, pady=6)
        self.loot_cost_canvas = tk.Canvas(graph2, height=210, background="#ffffff", highlightthickness=1, highlightbackground="#d7dce2")
        self.loot_cost_canvas.pack(fill="both", expand=True)

        graph3 = ttk.LabelFrame(right, text="3. Number of looted items / elapsed time (not cumulative)", padding=6)
        graph3.pack(fill="both", expand=True, pady=(6, 0))
        self.loot_items_time_canvas = tk.Canvas(graph3, height=210, background="#ffffff", highlightthickness=1, highlightbackground="#d7dce2")
        self.loot_items_time_canvas.pack(fill="both", expand=True)

        for canvas in (self.loot_value_time_canvas, self.loot_cost_canvas, self.loot_items_time_canvas):
            canvas.bind("<Configure>", lambda event, c=canvas: self.redraw_chart_canvas(c))
            canvas.bind("<ButtonPress-1>", lambda event, c=canvas: self.on_loot_chart_drag_start(c, event))
            canvas.bind("<B1-Motion>", lambda event, c=canvas: self.on_loot_chart_drag_motion(c, event))
            canvas.bind("<ButtonRelease-1>", lambda event, c=canvas: self.on_loot_chart_drag_end(c, event))

    def loot_source_session(self):
        if self.current_session is not None:
            # A shallow mapping is enough for read-only loot rendering. asdict()
            # recursively copied every saved event and became very expensive in
            # long sessions.
            return vars(self.current_session)
        selected = self.selected_session_from_table() if hasattr(self, "sessions_tree") else None
        if selected:
            return selected
        if self.sessions:
            return self.sessions[-1]
        return None

    def loot_events_for_session(self, session):
        if not session:
            return []
        events = list(session.get("loot_events", []) or [])
        if events:
            return self.sanitize_loot_events(events)
        # Backward compatibility for old saved sessions before loot_events existed.
        reconstructed = []
        previous_type = ""
        cost_per_attack = hunting_setup_cost_per_shot_ped(
            session.get("weapon", ""),
            session.get("amplifier", ""),
            session.get("attachments", []) or [],
        )
        running_cost = 0.0
        previous_event_cost = 0.0
        for event in session.get("events", []) or []:
            etype = event.get("type", "")
            if etype in ("normal_hit", "crit", "defended_attack", "miss", "jammed") and session.get("count_hunting", False):
                running_cost += cost_per_attack
            elif etype == "loot":
                if reconstructed and previous_type == "loot":
                    loot_event = reconstructed[-1]
                else:
                    cost_ped = max(0.0, running_cost - previous_event_cost)
                    previous_event_cost = running_cost
                    loot_event = {
                        "index": len(reconstructed) + 1,
                        "started_at": event.get("timestamp", ""),
                        "ended_at": event.get("timestamp", ""),
                        "value_ped": 0.0,
                        "cost_ped": cost_ped,
                        "items": {},
                        "messages": [],
                    }
                    reconstructed.append(loot_event)
                item = event.get("item") or normalize_loot_item_name(str(event.get("message", "")).replace("You received", "").split("Value:")[0])
                if ignored_loot_item_name(item):
                    previous_type = etype
                    continue
                value_ped = float(event.get("value_ped", 0.0) or 0.0)
                quantity = int(event.get("quantity", 0) or 0)
                if quantity <= 1 and item in STACKABLE_ITEM_PED_VALUE:
                    quantity = parse_loot_item_quantity(item, item, value_ped)
                if quantity <= 0:
                    quantity = 1
                loot_event["ended_at"] = event.get("timestamp", loot_event.get("ended_at", ""))
                loot_event["value_ped"] = float(loot_event.get("value_ped", 0.0)) + value_ped
                loot_event.setdefault("items", {})[item] = int(loot_event.setdefault("items", {}).get(item, 0)) + quantity
                loot_event.setdefault("messages", []).append(event.get("message", ""))
            previous_type = etype
        return reconstructed

    def sanitize_loot_events(self, loot_events):
        """Apply ignored loot filtering and stackable quantity fixes to saved events.

        This also fixes older saved sessions made by versions that counted
        ignored loot or saved Shrapnel as x1 because chat.log does not print its
        stack count.
        """
        result = []
        loot_message_re = re.compile(r"^You received (.+?) Value: ([0-9]+(?:\.[0-9]+)?) PED$")
        for original in list(loot_events or []):
            event = dict(original or {})
            messages = list(event.get("messages", []) or [])
            rebuilt_items = {}
            rebuilt_value = 0.0
            rebuilt_from_messages = False
            for message in messages:
                match = loot_message_re.match(str(message or ""))
                if not match:
                    continue
                raw_item_name = match.group(1).strip()
                item_name = normalize_loot_item_name(raw_item_name)
                value_ped = float(match.group(2))
                if ignored_loot_item_name(item_name):
                    rebuilt_from_messages = True
                    continue
                quantity = parse_loot_item_quantity(raw_item_name, item_name, value_ped)
                rebuilt_items[item_name] = int(rebuilt_items.get(item_name, 0)) + int(quantity)
                rebuilt_value += value_ped
                rebuilt_from_messages = True

            if rebuilt_from_messages:
                event["items"] = rebuilt_items
                event["value_ped"] = rebuilt_value
            else:
                filtered_items = {}
                for item, quantity in (event.get("items", {}) or {}).items():
                    if ignored_loot_item_name(item):
                        continue
                    fixed_quantity = int(quantity or 0)
                    if item in STACKABLE_ITEM_PED_VALUE and fixed_quantity <= 1:
                        try:
                            fixed_quantity = parse_loot_item_quantity(item, item, float(event.get("value_ped", 0.0) or 0.0))
                        except Exception:
                            fixed_quantity = int(quantity or 0)
                    filtered_items[item] = filtered_items.get(item, 0) + fixed_quantity
                event["items"] = filtered_items

            if float(event.get("value_ped", 0.0) or 0.0) <= 0 and not event.get("items"):
                continue
            event["index"] = len(result) + 1
            result.append(event)
        return result

    def loot_item_totals(self, loot_events):
        totals = {}
        for loot_event in loot_events:
            for item, quantity in (loot_event.get("items", {}) or {}).items():
                totals[item] = totals.get(item, 0) + int(quantity or 0)
        return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0].lower())))

    def loot_item_value_totals(self, loot_events):
        """Group loot by item name with summed quantity and TT value.

        Current sessions preserve each original loot message, so item values are
        exact even when several items belong to the same loot event. For very old
        sessions without messages, the event value is assigned directly when
        there is one item, or proportionally by quantity as a best-effort fallback.
        """
        totals = {}
        loot_message_re = re.compile(r"^You received (.+?) Value: ([0-9]+(?:\.[0-9]+)?) PED$")

        def add(item_name, quantity, value_ped):
            if ignored_loot_item_name(item_name):
                return
            row = totals.setdefault(item_name, {"quantity": 0, "value_ped": 0.0})
            row["quantity"] += max(0, int(quantity or 0))
            row["value_ped"] += max(0.0, float(value_ped or 0.0))

        for loot_event in list(loot_events or []):
            parsed_messages = 0
            for message in list(loot_event.get("messages", []) or []):
                match = loot_message_re.match(str(message or ""))
                if not match:
                    continue
                raw_item_name = match.group(1).strip()
                item_name = normalize_loot_item_name(raw_item_name)
                value_ped = float(match.group(2))
                quantity = parse_loot_item_quantity(raw_item_name, item_name, value_ped)
                add(item_name, quantity, value_ped)
                parsed_messages += 1

            if parsed_messages:
                continue

            items = {
                str(item): max(0, int(quantity or 0))
                for item, quantity in (loot_event.get("items", {}) or {}).items()
                if not ignored_loot_item_name(item)
            }
            if not items:
                continue
            event_value = max(0.0, float(loot_event.get("value_ped", 0.0) or 0.0))
            if len(items) == 1:
                item_name, quantity = next(iter(items.items()))
                add(item_name, quantity, event_value)
                continue

            total_quantity = sum(items.values())
            for item_name, quantity in items.items():
                allocated_value = event_value * quantity / total_quantity if total_quantity else 0.0
                add(item_name, quantity, allocated_value)

        return dict(sorted(totals.items(), key=lambda item: (-item[1]["value_ped"], item[0].lower())))

    def loot_item_drop_details(self, item_name: str, loot_events):
        """Return every loot-event occurrence of one grouped item.

        Values are exact when the original loot messages are available. Older
        sessions use the same best-effort value allocation as the event and
        grouped-item summaries, so all three views stay consistent.
        """
        rows = []
        for loot_event in list(loot_events or []):
            item_details = self.loot_event_item_details(loot_event)
            item_row = item_details.get(item_name)
            if not item_row:
                continue
            rows.append({
                "event_index": loot_event.get("index", len(rows) + 1),
                "started_at": loot_event.get("started_at", ""),
                "ended_at": loot_event.get("ended_at", loot_event.get("started_at", "")),
                "quantity": int(item_row.get("quantity", 0) or 0),
                "value_ped": float(item_row.get("value_ped", 0.0) or 0.0),
                "event_value_ped": float(loot_event.get("value_ped", 0.0) or 0.0),
                "event_cost_ped": float(loot_event.get("cost_ped", 0.0) or 0.0),
                "messages": list(item_row.get("messages", []) or []),
            })
        return rows

    def loot_event_item_details(self, loot_event):
        """Return exact per-item quantity and value details for one loot event.

        Newer sessions retain every original loot message, which provides exact
        value-per-item data. Old sessions without messages use the same
        quantity-proportional fallback as the grouped loot summary.
        """
        details = {}

        def add(item_name, quantity, value_ped, message=""):
            if ignored_loot_item_name(item_name):
                return
            row = details.setdefault(
                item_name,
                {"quantity": 0, "value_ped": 0.0, "messages": []},
            )
            row["quantity"] += max(0, int(quantity or 0))
            row["value_ped"] += max(0.0, float(value_ped or 0.0))
            if message:
                row["messages"].append(str(message))

        parsed_messages = 0
        for message in list((loot_event or {}).get("messages", []) or []):
            match = ChatLogParser.loot_re.match(str(message or ""))
            if not match:
                continue
            raw_item_name = match.group(1).strip()
            item_name = normalize_loot_item_name(raw_item_name)
            value_ped = float(match.group(2))
            quantity = parse_loot_item_quantity(raw_item_name, item_name, value_ped)
            add(item_name, quantity, value_ped, message)
            parsed_messages += 1

        if not parsed_messages:
            items = {
                str(item): max(0, int(quantity or 0))
                for item, quantity in ((loot_event or {}).get("items", {}) or {}).items()
                if not ignored_loot_item_name(item)
            }
            event_value = max(0.0, float((loot_event or {}).get("value_ped", 0.0) or 0.0))
            if len(items) == 1:
                item_name, quantity = next(iter(items.items()))
                add(item_name, quantity, event_value)
            elif items:
                total_quantity = sum(items.values())
                for item_name, quantity in items.items():
                    allocated_value = event_value * quantity / total_quantity if total_quantity else 0.0
                    add(item_name, quantity, allocated_value)

        return dict(sorted(details.items(), key=lambda pair: (-pair[1]["value_ped"], pair[0].lower())))

    def open_loot_event_details(self, event=None):
        tree = self.loot_events_tree
        iid = tree.identify_row(event.y) if event is not None else ""
        if not iid:
            selected = tree.selection()
            iid = selected[0] if selected else ""
        loot_event = self.loot_event_iid_to_event.get(iid)
        if not loot_event:
            return
        tree.selection_set(iid)
        tree.focus(iid)

        item_details = self.loot_event_item_details(loot_event)
        event_index = loot_event.get("index", "")
        started_at = loot_event.get("started_at", "")
        ended_at = loot_event.get("ended_at", started_at)
        loot_value = float(loot_event.get("value_ped", 0.0) or 0.0)
        cost_ped = float(loot_event.get("cost_ped", 0.0) or 0.0)
        after_mu = sum(
            self.loot_value_after_markup(
                item_name,
                float(row.get("value_ped", 0.0) or 0.0),
                int(row.get("quantity", 0) or 0),
            )
            for item_name, row in item_details.items()
        )

        window = tk.Toplevel(self.root)
        window.title(f"Loot event #{event_index} details")
        window.geometry("900x560")
        window.minsize(720, 420)
        window.transient(self.root)

        summary = ttk.LabelFrame(window, text="Event summary", padding=10)
        summary.pack(fill="x", padx=10, pady=10)
        ttk.Label(
            summary,
            text=(
                f"Event: #{event_index} | Start: {started_at or '-'} | End: {ended_at or '-'}\n"
                f"Cost: {cost_ped:.6f} PED | TT loot: {loot_value:.4f} PED "
                f"({percent(loot_value, cost_ped):.2f}% return) | "
                f"After MU: {after_mu:.4f} PED ({percent(after_mu, cost_ped):.2f}% return)"
            ),
            justify="left",
        ).pack(anchor="w")

        table_frame = ttk.LabelFrame(window, text="Items", padding=6)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        columns = ("item", "quantity", "value", "event_percent", "markup", "after_mu")
        detail_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        setup = [
            ("item", "Item", 300),
            ("quantity", "Quantity", 90),
            ("value", "TT value PED", 110),
            ("event_percent", "% of event", 105),
            ("markup", "MU", 105),
            ("after_mu", "Value after MU", 125),
        ]
        for column, title, width in setup:
            detail_tree.heading(column, text=title)
            detail_tree.column(column, width=width, anchor="w" if column == "item" else "center")
        detail_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=detail_tree.yview)
        detail_tree.configure(yscrollcommand=detail_scroll.set)
        detail_tree.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        for item_name, row in item_details.items():
            quantity = int(row.get("quantity", 0) or 0)
            value_ped = float(row.get("value_ped", 0.0) or 0.0)
            markup_display = self.loot_markup_display(item_name)
            item_after_mu = self.loot_value_after_markup(item_name, value_ped, quantity)
            detail_tree.insert(
                "",
                "end",
                values=(
                    item_name,
                    quantity,
                    f"{value_ped:.4f}",
                    f"{percent(value_ped, loot_value):.2f}%",
                    markup_display,
                    f"{item_after_mu:.4f}",
                ),
            )

        messages_frame = ttk.LabelFrame(window, text="Original loot messages", padding=6)
        messages_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        message_text = tk.Text(messages_frame, height=7, wrap="none")
        message_y = ttk.Scrollbar(messages_frame, orient="vertical", command=message_text.yview)
        message_x = ttk.Scrollbar(messages_frame, orient="horizontal", command=message_text.xview)
        message_text.configure(yscrollcommand=message_y.set, xscrollcommand=message_x.set)
        message_text.grid(row=0, column=0, sticky="nsew")
        message_y.grid(row=0, column=1, sticky="ns")
        message_x.grid(row=1, column=0, sticky="ew")
        messages_frame.rowconfigure(0, weight=1)
        messages_frame.columnconfigure(0, weight=1)
        messages = list(loot_event.get("messages", []) or [])
        if messages:
            for message in messages:
                message_text.insert("end", str(message) + "\n")
        else:
            message_text.insert("end", "Original messages are unavailable for this older saved event.\n")
        message_text.configure(state="disabled")

    def skill_gain_message_details(self, session, skill_name: str):
        """Build chronological per-message skill details for current and old sessions."""
        if isinstance(session, MonitorSession):
            session = asdict(session)
        session = session or {}
        start_snapshot = session.get("current_skills_at_start", {}) or {}
        current_points = parse_float(start_snapshot.get(skill_name), 0.0)
        details = []
        for event in list(session.get("events", []) or []):
            if event.get("type") != "skill_gain" or str(event.get("skill", "")) != str(skill_name):
                continue
            point_gain = parse_float(event.get("delta_points", event.get("delta_tt", 0.0)), 0.0)
            old_points = parse_float(event.get("skill_points_before"), current_points)
            new_points = parse_float(event.get("skill_points_after"), old_points + point_gain)
            stored_tt_gain = event.get("tt_gain")
            if stored_tt_gain is None:
                try:
                    tt_gain = skill_tt_value(new_points) - skill_tt_value(old_points)
                except Exception:
                    tt_gain = 0.0
            else:
                tt_gain = parse_float(stored_tt_gain, 0.0)
            details.append({
                "timestamp": event.get("timestamp", ""),
                "point_gain": point_gain,
                "tt_gain": tt_gain,
                "old_points": old_points,
                "new_points": new_points,
                "message": event.get("message", ""),
            })
            current_points = new_points
        return details

    def open_skill_gain_details_from_tree(self, event, tree, source="active"):
        iid = tree.identify_row(event.y) if event is not None else ""
        if not iid:
            selected = tree.selection()
            iid = selected[0] if selected else ""
        if not iid:
            return
        values = tree.item(iid, "values")
        if not values:
            return
        skill_name = str(values[0])
        tree.selection_set(iid)
        tree.focus(iid)

        if source == "active":
            session = self.current_session
            source_text = "active session"
        else:
            session = self.selected_session_from_table()
            source_text = "selected saved session"
        if session is None:
            messagebox.showwarning("No session", "There is no session available for this skill.")
            return

        details = self.skill_gain_message_details(session, skill_name)
        session_data = asdict(session) if isinstance(session, MonitorSession) else session
        total_points = sum(float(row.get("point_gain", 0.0) or 0.0) for row in details)
        total_tt = sum(float(row.get("tt_gain", 0.0) or 0.0) for row in details)
        start_points = parse_float((session_data.get("current_skills_at_start", {}) or {}).get(skill_name), 0.0)
        end_points = details[-1]["new_points"] if details else start_points

        window = tk.Toplevel(self.root)
        window.title(f"{skill_name} gain messages")
        window.geometry("1100x560")
        window.minsize(820, 400)
        window.transient(self.root)

        summary = ttk.LabelFrame(window, text="Skill summary", padding=10)
        summary.pack(fill="x", padx=10, pady=10)
        ttk.Label(
            summary,
            text=(
                f"Skill: {skill_name} | Source: {source_text} | Messages: {len(details)}\n"
                f"Total gain: {total_points:.6f} points / {total_tt:.8f} TT-equivalent | "
                f"Skill: {start_points:.6f} -> {end_points:.6f}"
            ),
            justify="left",
        ).pack(anchor="w")

        table_frame = ttk.LabelFrame(window, text="Each skill gain message", padding=6)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        columns = ("number", "time", "points", "tt", "before", "after", "message")
        detail_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        setup = [
            ("number", "#", 45),
            ("time", "Time", 145),
            ("points", "Point gain", 95),
            ("tt", "TT-equivalent", 115),
            ("before", "Before", 100),
            ("after", "After", 100),
            ("message", "Original message", 430),
        ]
        for column, title, width in setup:
            detail_tree.heading(column, text=title)
            detail_tree.column(column, width=width, anchor="w" if column == "message" else "center")
        detail_y = ttk.Scrollbar(table_frame, orient="vertical", command=detail_tree.yview)
        detail_x = ttk.Scrollbar(table_frame, orient="horizontal", command=detail_tree.xview)
        detail_tree.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)
        detail_tree.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        for index, row in enumerate(details, start=1):
            detail_tree.insert(
                "",
                "end",
                values=(
                    index,
                    row.get("timestamp", ""),
                    f"{float(row.get('point_gain', 0.0) or 0.0):.6f}",
                    f"{float(row.get('tt_gain', 0.0) or 0.0):.8f}",
                    f"{float(row.get('old_points', 0.0) or 0.0):.6f}",
                    f"{float(row.get('new_points', 0.0) or 0.0):.6f}",
                    row.get("message", ""),
                ),
            )

        if not details:
            detail_tree.insert("", "end", values=("", "", "", "", "", "", "No saved gain messages for this skill."))

    def reload_market_data_if_changed(self):
        """Reload market_data.json after the clipboard collector updates it."""
        try:
            current_mtime_ns = MARKET_DATA_FILE.stat().st_mtime_ns
        except OSError:
            current_mtime_ns = 0
        if current_mtime_ns == self.market_data_mtime_ns:
            return
        self.market_weekly_markups = load_weekly_market_markups(MARKET_DATA_FILE)
        self.market_data_mtime_ns = current_mtime_ns

    def loot_markup_info_for_item(self, item_name: str):
        """Return effective MU data, with manual values taking priority."""
        if item_name in self.loot_markups:
            try:
                return {
                    "type": "percentage",
                    "value": max(100.0, float(self.loot_markups[item_name])),
                    "source": "manual",
                }
            except (TypeError, ValueError):
                pass

        market_markup = self.market_weekly_markups.get(item_name)
        if isinstance(market_markup, dict):
            try:
                value = float(market_markup.get("value"))
            except (TypeError, ValueError):
                value = None
            markup_type = market_markup.get("type")
            if markup_type == "percentage" and value is not None and value >= 100.0:
                return {"type": "percentage", "value": value, "source": "weekly market"}
            if markup_type == "fixed" and value is not None and value >= 0.0:
                return {"type": "fixed", "value": value, "source": "weekly market"}

        return {"type": "percentage", "value": 100.0, "source": "default"}

    def loot_markup_for_item(self, item_name: str) -> float:
        """Backward-compatible percentage accessor used by the manual editor."""
        info = self.loot_markup_info_for_item(item_name)
        if info["type"] == "percentage":
            return float(info["value"])
        return 100.0

    def loot_markup_display(self, item_name: str) -> str:
        info = self.loot_markup_info_for_item(item_name)
        if info["type"] == "fixed":
            text = f"TT+{float(info['value']):g} PED"
        else:
            text = f"{float(info['value']):.2f}%"
        if info.get("source") == "weekly market":
            text += " (W)"
        return text

    def loot_value_after_markup(self, item_name: str, value_ped: float, quantity: int = 1) -> float:
        info = self.loot_markup_info_for_item(item_name)
        value_ped = max(0.0, float(value_ped or 0.0))
        quantity = max(0, int(quantity or 0))
        if info["type"] == "fixed":
            return value_ped + float(info["value"]) * quantity
        return value_ped * float(info["value"]) / 100.0

    def save_loot_markups(self):
        save_json(
            LOOT_MARKUPS_FILE,
            {item: float(value) for item, value in sorted(self.loot_markups.items(), key=lambda pair: pair[0].lower())},
        )

    def refresh_loot_item_summary_table(self, item_stats, loot_total: float) -> float:
        if not hasattr(self, "loot_item_summary_tree"):
            return float(loot_total or 0.0)
        if self.loot_markup_editor is not None:
            self.cancel_loot_markup_edit()
        self.loot_item_summary_tree.delete(*self.loot_item_summary_tree.get_children())
        self.loot_item_summary_iid_to_name = {}
        total_after_mu = 0.0
        for index, (item_name, data) in enumerate(item_stats.items()):
            quantity = int(data.get("quantity", 0) or 0)
            value_ped = float(data.get("value_ped", 0.0) or 0.0)
            markup_display = self.loot_markup_display(item_name)
            after_mu = self.loot_value_after_markup(item_name, value_ped, quantity)
            total_after_mu += after_mu
            iid = f"loot_item_summary_{index}"
            self.loot_item_summary_iid_to_name[iid] = item_name
            self.loot_item_summary_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    item_name,
                    quantity,
                    f"{value_ped:.4f}",
                    f"{percent(value_ped, loot_total):.2f}%",
                    markup_display,
                    f"{after_mu:.4f}",
                ),
            )
        self.apply_tree_sort(self.loot_item_summary_tree)
        return total_after_mu

    def on_loot_item_summary_double_click(self, event):
        """Edit MU when its cell is clicked; otherwise open item drop history."""
        tree = self.loot_item_summary_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) == "#5":
            self.begin_loot_markup_edit(event)
        else:
            self.open_loot_item_details(event)

    def open_loot_item_details(self, event=None):
        tree = self.loot_item_summary_tree
        iid = tree.identify_row(event.y) if event is not None else ""
        if not iid:
            selected = tree.selection()
            iid = selected[0] if selected else ""
        item_name = self.loot_item_summary_iid_to_name.get(iid)
        if not iid or not item_name:
            return
        tree.selection_set(iid)
        tree.focus(iid)

        session = self.loot_source_session()
        loot_events = self.loot_events_for_session(session)
        drops = self.loot_item_drop_details(item_name, loot_events)
        total_loot = sum(float(row.get("value_ped", 0.0) or 0.0) for row in loot_events)
        ped_cycled = float((session or {}).get("ped_cycled", 0.0) or 0.0)
        total_quantity = sum(int(row.get("quantity", 0) or 0) for row in drops)
        total_value = sum(float(row.get("value_ped", 0.0) or 0.0) for row in drops)
        markup_display = self.loot_markup_display(item_name)
        total_after_mu = self.loot_value_after_markup(item_name, total_value, total_quantity)

        window = tk.Toplevel(self.root)
        window.title(f"{item_name} loot details")
        window.geometry("1120x600")
        window.minsize(840, 430)
        window.transient(self.root)

        summary = ttk.LabelFrame(window, text="Item summary", padding=10)
        summary.pack(fill="x", padx=10, pady=10)
        ttk.Label(
            summary,
            text=(
                f"Item: {item_name} | Loot events containing item: {len(drops)} | Total quantity: {total_quantity}\n"
                f"TT value: {total_value:.4f} PED ({percent(total_value, total_loot):.2f}% of total loot) | "
                f"MU: {markup_display} | After MU: {total_after_mu:.4f} PED "
                f"({percent(total_after_mu, ped_cycled):.2f}% of PED cycled)"
            ),
            justify="left",
        ).pack(anchor="w")

        table_frame = ttk.LabelFrame(window, text="Every drop", padding=6)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        columns = ("event", "start", "end", "quantity", "value", "item_percent", "event_percent", "markup", "after_mu", "message")
        detail_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=17)
        setup = [
            ("event", "Event #", 65),
            ("start", "Start time", 145),
            ("end", "End time", 145),
            ("quantity", "Quantity", 75),
            ("value", "TT value PED", 95),
            ("item_percent", "% of item total", 105),
            ("event_percent", "% of event", 90),
            ("markup", "MU", 100),
            ("after_mu", "After MU", 90),
            ("message", "Original message(s)", 360),
        ]
        for column, title, width in setup:
            detail_tree.heading(column, text=title)
            detail_tree.column(column, width=width, anchor="w" if column == "message" else "center")
        detail_y = ttk.Scrollbar(table_frame, orient="vertical", command=detail_tree.yview)
        detail_x = ttk.Scrollbar(table_frame, orient="horizontal", command=detail_tree.xview)
        detail_tree.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)
        detail_tree.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        for row in drops:
            value_ped = float(row.get("value_ped", 0.0) or 0.0)
            event_value = float(row.get("event_value_ped", 0.0) or 0.0)
            item_after_mu = self.loot_value_after_markup(
                item_name, value_ped, int(row.get("quantity", 0) or 0)
            )
            messages = " | ".join(str(message) for message in row.get("messages", []) if message)
            if not messages:
                messages = "Original message unavailable for this older saved event."
            detail_tree.insert(
                "",
                "end",
                values=(
                    row.get("event_index", ""),
                    row.get("started_at", ""),
                    row.get("ended_at", ""),
                    int(row.get("quantity", 0) or 0),
                    f"{value_ped:.4f}",
                    f"{percent(value_ped, total_value):.2f}%",
                    f"{percent(value_ped, event_value):.2f}%",
                    markup_display,
                    f"{item_after_mu:.4f}",
                    messages,
                ),
            )

        if not drops:
            detail_tree.insert("", "end", values=("", "", "", "", "", "", "", "", "", "No saved drops found for this item."))

    def begin_loot_markup_edit(self, event):
        tree = self.loot_item_summary_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != "#5":
            return
        iid = tree.identify_row(event.y)
        item_name = self.loot_item_summary_iid_to_name.get(iid)
        if not iid or not item_name:
            return
        bbox = tree.bbox(iid, "markup")
        if not bbox:
            return
        self.cancel_loot_markup_edit()
        x, y, width, height = bbox
        editor = ttk.Entry(tree, justify="center")
        editor.insert(0, f"{float(self.loot_markups.get(item_name, self.loot_markup_for_item(item_name))):g}")
        editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        self.loot_markup_editor = editor
        self.loot_markup_editor_item = item_name
        editor.bind("<Return>", self.commit_loot_markup_edit)
        editor.bind("<Escape>", self.cancel_loot_markup_edit)
        editor.bind("<FocusOut>", self.commit_loot_markup_edit)
        editor.focus_set()

    def commit_loot_markup_edit(self, event=None):
        editor = self.loot_markup_editor
        if editor is None:
            return
        item_name = getattr(self, "loot_markup_editor_item", "")
        raw_value = editor.get().strip().replace("%", "").replace(",", ".")
        self.loot_markup_editor = None
        self.loot_markup_editor_item = ""
        editor.destroy()
        try:
            markup = float(raw_value)
        except ValueError:
            self.loot_markup_status_var.set(f"Invalid MU for {item_name}. Enter a number such as 101 or 125.5.")
            return
        if markup < 100.0:
            self.loot_markup_status_var.set(f"MU for {item_name} must be 100% or higher.")
            return
        self.loot_markups[item_name] = markup
        self.save_loot_markups()
        self.loot_markup_status_var.set(f"Saved manual override for {item_name}: {markup:g}% in {LOOT_MARKUPS_FILE}.")
        self.refresh_loot_tab()

    def cancel_loot_markup_edit(self, event=None):
        editor = self.loot_markup_editor
        self.loot_markup_editor = None
        self.loot_markup_editor_item = ""
        if editor is not None:
            try:
                editor.destroy()
            except tk.TclError:
                pass

    def selected_loot_items(self):
        return [item for item, var in self.loot_item_vars.items() if var.get()]

    def clear_loot_item_selection(self):
        for var in self.loot_item_vars.values():
            var.set(False)
        self.refresh_loot_tab()

    def refresh_loot_item_checks(self, force=False):
        """Rebuild the loot item checkbox list only when it actually changed.

        Recreating dozens/hundreds of Tk checkboxes on every parsed log batch is
        expensive and was one of the main reasons the tracker UI felt slow.
        """
        session = self.loot_source_session()
        loot_events = self.loot_events_for_session(session)
        totals = self.loot_item_totals(loot_events)
        query = self.loot_item_filter_var.get().strip().lower()
        signature = (tuple(totals.items()), query)
        if not force and signature == self.loot_item_check_signature:
            return

        selected = set(self.selected_loot_items())
        for child in self.loot_items_scrollable.winfo_children():
            child.destroy()
        self.loot_item_vars = {item: tk.BooleanVar(value=item in selected) for item in totals}
        for item, quantity in totals.items():
            if query and query not in item.lower():
                continue
            ttk.Checkbutton(
                self.loot_items_scrollable,
                text=f"{item} ({quantity})",
                variable=self.loot_item_vars[item],
                command=self.refresh_loot_tab,
            ).pack(anchor="w")
        self.loot_item_check_signature = signature

    def schedule_loot_refresh(self, delay_ms=750):
        """Debounce expensive loot redraws and skip them while the tab is hidden."""
        if not hasattr(self, "loot_value_time_canvas") or not self.is_tab_active(self.loot_tab):
            return
        if self.loot_refresh_after_id is not None:
            return
        if self.reader_active:
            delay_ms = max(int(delay_ms), int(self.loot_live_refresh_interval * 1000))
        self.loot_refresh_after_id = self.root.after(delay_ms, self._run_scheduled_loot_refresh)

    def _run_scheduled_loot_refresh(self):
        self.loot_refresh_after_id = None
        if self.is_tab_active(self.loot_tab):
            self.refresh_loot_tab()
            self.last_loot_live_refresh_at = time.monotonic()

    def first_loot_event_time(self, loot_events):
        for loot_event in loot_events:
            event_time = parse_any_timestamp(loot_event.get("started_at") or loot_event.get("ended_at"))
            if event_time is not None:
                return event_time
            event_time = parse_any_timestamp(loot_event.get("ended_at") or loot_event.get("started_at"))
            if event_time is not None:
                return event_time
        return None

    def downsample_points(self, points, max_points=1200):
        points = list(points or [])
        if len(points) <= max_points:
            return points
        if max_points < 3:
            return points[:max_points]
        step = (len(points) - 1) / float(max_points - 1)
        sampled = []
        last_index = -1
        for i in range(max_points):
            index = int(round(i * step))
            if index != last_index:
                sampled.append(points[index])
                last_index = index
        return sampled

    def downsample_time_points_by_minute(self, points, bucket_minutes=1.0, max_points=1500):
        """Keep the latest point in each elapsed-time bucket.

        Graph 1 can easily have thousands of loot events. Drawing every dot and
        line segment makes Tkinter slow, while one point per minute is enough to
        see the return trend.
        """
        points = list(points or [])
        if not points:
            return []
        if bucket_minutes <= 0:
            return self.downsample_points(points, max_points)
        sampled_by_bucket = {}
        for point in points:
            try:
                bucket = int(float(point.get("x", 0.0)) // float(bucket_minutes))
            except (TypeError, ValueError):
                bucket = len(sampled_by_bucket)
            sampled_by_bucket[bucket] = point
        sampled = list(sampled_by_bucket.values())
        if points[0] not in sampled:
            sampled.insert(0, points[0])
        if points[-1] not in sampled:
            sampled.append(points[-1])
        return self.downsample_points(sampled, max_points)

    def refresh_loot_tab(self):
        if not hasattr(self, "loot_value_time_canvas"):
            return
        self.reload_market_data_if_changed()
        session = self.loot_source_session()
        loot_events = self.loot_events_for_session(session)
        self.refresh_loot_item_checks()
        self.loot_events_tree.delete(*self.loot_events_tree.get_children())
        self.loot_event_iid_to_event = {}

        if not session:
            self.loot_summary_var.set("No active or saved session yet.")
            if hasattr(self, "loot_item_summary_tree"):
                self.loot_item_summary_tree.delete(*self.loot_item_summary_tree.get_children())
                self.loot_item_summary_iid_to_name = {}
            self.clear_chart(self.loot_value_time_canvas, "No loot data")
            self.clear_chart(self.loot_cost_canvas, "No loot data")
            self.clear_chart(self.loot_items_time_canvas, "No loot data")
            return

        ped_cycled = float(session.get("ped_cycled", 0.0) or 0.0)
        loot_total = sum(float(event.get("value_ped", 0.0) or 0.0) for event in loot_events)
        cost_per_kill = ped_cycled / len(loot_events) if loot_events else 0.0
        item_totals = self.loot_item_totals(loot_events)
        item_value_totals = self.loot_item_value_totals(loot_events)
        loot_after_mu = self.refresh_loot_item_summary_table(item_value_totals, loot_total)
        top_items = "; ".join(f"{item}: {qty}" for item, qty in list(item_totals.items())[:6]) or "-"
        source = "active session" if self.current_session is not None else "saved session"
        self.loot_summary_var.set(
            f"Source: {source} | Loot events/kills: {len(loot_events)} | PED cycled: {ped_cycled:.4f} | "
            f"TT loot: {loot_total:.4f} PED ({percent(loot_total, ped_cycled):.2f}%) | "
            f"Loot after MU: {loot_after_mu:.4f} PED ({percent(loot_after_mu, ped_cycled):.2f}% of cycled)\n"
            f"Cost per kill/event: {cost_per_kill:.6f} PED | Unique items: {len(item_value_totals)} | Top items: {top_items}"
        )

        # Show every saved loot event. Earlier versions only displayed the last
        # 500 rows, which looked like older loot events were not saved.
        # The graphs are still downsampled separately, so drawing performance is
        # not tied to the number of rows shown here.
        for row_index, loot_event in enumerate(loot_events):
            cost_ped = float(loot_event.get("cost_ped", 0.0) or 0.0)
            iid = f"loot_event_{row_index}"
            self.loot_event_iid_to_event[iid] = loot_event
            self.loot_events_tree.insert("", "end", iid=iid, values=(
                loot_event.get("index", ""),
                loot_event.get("ended_at", loot_event.get("started_at", "")),
                sum(int(v or 0) for v in (loot_event.get("items", {}) or {}).values()),
                f"{float(loot_event.get('value_ped', 0.0) or 0.0):.4f}",
                f"{cost_ped:.4f}",
                f"{(float(loot_event.get('value_ped', 0.0) or 0.0) / cost_ped):.2f}x" if cost_ped else "",
            ))
        self.apply_tree_sort(self.loot_events_tree)

        # Use the first real loot timestamp, not the app session start time.
        # When you resync old chat.log lines, the session start can be later
        # than the loot timestamps, which made every elapsed-time X value clamp
        # to 0 and caused the item graph to draw one vertical line.
        base_time = self.first_loot_event_time(loot_events)
        has_real_time_axis = base_time is not None

        # Graph 1: cumulative loot return over elapsed loot time.
        cumulative_loot = 0.0
        cumulative_cost = 0.0
        loot_value_points = []
        for index, loot_event in enumerate(loot_events, start=1):
            value_ped = float(loot_event.get("value_ped", 0.0) or 0.0)
            event_cost = float(loot_event.get("cost_ped", 0.0) or 0.0)
            if event_cost <= 0 and cost_per_kill > 0:
                event_cost = cost_per_kill
            cumulative_loot += value_ped
            cumulative_cost += event_cost
            event_time = parse_any_timestamp(loot_event.get("ended_at") or loot_event.get("started_at"))
            x_value = self.elapsed_minutes(base_time, event_time, index) if has_real_time_axis else float(index)
            denominator = cumulative_cost if cumulative_cost > 0 else ped_cycled
            loot_value_points.append({
                "x": x_value,
                "y": percent(cumulative_loot, denominator),
                "label": self.format_chart_time_label(event_time, index) if has_real_time_axis else str(index),
            })
        self.render_line_chart(
            self.loot_value_time_canvas,
            self.downsample_time_points_by_minute(loot_value_points, bucket_minutes=1.0),
            title="Cumulative loot / cumulative cost",
            x_label="Elapsed time" if has_real_time_axis else "Loot event #",
            y_label="Loot return",
            x_is_time=has_real_time_axis,
            y_suffix="%",
            smooth=False,
            y_reference_lines=[(100.0, "100%")],
        )

        scatter_points = []
        for index, loot_event in enumerate(loot_events, start=1):
            cost_ped = float(loot_event.get("cost_ped", 0.0) or 0.0)
            value_ped = float(loot_event.get("value_ped", 0.0) or 0.0)
            event_time = parse_any_timestamp(loot_event.get("ended_at") or loot_event.get("started_at"))
            scatter_points.append({
                "x": cost_ped,
                "y": value_ped,
                "label": self.format_chart_time_label(event_time, index),
            })
        self.render_scatter_chart(
            self.loot_cost_canvas,
            self.downsample_points(scatter_points, max_points=1500),
            title="Loot value vs cost per kill",
            x_label="Cost per kill (PED)",
            y_label="Loot value (PED)",
            y_suffix=" PED",
            draw_break_even=True,
            draw_multiplier_lines=True,
        )

        selected_items = self.selected_loot_items()
        if not selected_items:
            selected_items = list(item_totals.keys())[:5]
        item_series = []
        for item in selected_items[:12]:
            raw_points = []
            for index, loot_event in enumerate(loot_events, start=1):
                quantity = int((loot_event.get("items", {}) or {}).get(item, 0) or 0)
                if quantity <= 0:
                    continue
                event_time = parse_any_timestamp(loot_event.get("ended_at") or loot_event.get("started_at"))
                raw_points.append({
                    "x": self.elapsed_minutes(base_time, event_time, index) if has_real_time_axis else float(index),
                    "y": quantity,
                    "label": self.format_chart_time_label(event_time, index) if has_real_time_axis else str(index),
                })
            if raw_points:
                if has_real_time_axis:
                    buckets = {}
                    for point in raw_points:
                        bucket = int(float(point.get("x", 0.0)) // 1.0)
                        if bucket not in buckets:
                            buckets[bucket] = {"x": float(bucket), "y": 0, "label": self._time_axis_label(float(bucket))}
                        buckets[bucket]["y"] += int(point.get("y", 0) or 0)
                    points = list(buckets.values())
                else:
                    points = raw_points
                item_series.append((item, self.downsample_points(points, 800)))
        self.render_multi_point_chart(
            self.loot_items_time_canvas,
            item_series,
            title="Looted item quantity by time",
            x_label="Elapsed time" if has_real_time_axis else "Loot event #",
            y_label="Items looted",
            x_is_time=has_real_time_axis,
        )

    def clear_chart(self, canvas, text="No data"):
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 170)
        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")
        canvas.create_text(width / 2, height / 2, text=text, fill="#667085")

    def redraw_chart_canvas(self, canvas):
        payload = self.loot_chart_payloads.get(canvas)
        if not payload:
            self.clear_chart(canvas, "No loot data")
            return
        kind = payload.get("kind")
        if kind == "line":
            self._draw_line_chart_payload(canvas, payload)
        elif kind == "scatter":
            self._draw_scatter_chart_payload(canvas, payload)
        elif kind == "multi_line":
            self._draw_multi_line_chart_payload(canvas, payload)
        elif kind == "multi_points":
            self._draw_multi_point_chart_payload(canvas, payload)
        else:
            self.clear_chart(canvas, "No loot data")

    def reset_loot_zoom(self):
        self.loot_zoom_ranges = {}
        self.loot_selection_var.set("Zoom reset. Drag across any loot graph to zoom and show totals for the selected range.")
        self.refresh_loot_tab()

    def on_loot_chart_drag_start(self, canvas, event):
        meta = self.loot_chart_meta.get(canvas)
        if not meta:
            return
        self.loot_drag = {"canvas": canvas, "start_x": event.x, "rect": None}

    def on_loot_chart_drag_motion(self, canvas, event):
        if not self.loot_drag or self.loot_drag.get("canvas") is not canvas:
            return
        meta = self.loot_chart_meta.get(canvas)
        if not meta:
            return
        rect_id = self.loot_drag.get("rect")
        if rect_id:
            canvas.delete(rect_id)
        left = meta["left"]
        right = meta["right"]
        top = meta["top"]
        bottom = meta["bottom"]
        x1 = max(left, min(right, self.loot_drag.get("start_x", event.x)))
        x2 = max(left, min(right, event.x))
        rect_id = canvas.create_rectangle(min(x1, x2), top, max(x1, x2), bottom, outline="#f59e0b", fill="#fde68a", stipple="gray25")
        self.loot_drag["rect"] = rect_id

    def on_loot_chart_drag_end(self, canvas, event):
        if not self.loot_drag or self.loot_drag.get("canvas") is not canvas:
            return
        meta = self.loot_chart_meta.get(canvas)
        drag = self.loot_drag
        self.loot_drag = None
        rect_id = drag.get("rect")
        if rect_id:
            canvas.delete(rect_id)
        if not meta:
            return
        start_x = drag.get("start_x", event.x)
        end_x = event.x
        if abs(end_x - start_x) < 8:
            return
        min_sel = self._canvas_x_to_data(canvas, min(start_x, end_x), meta)
        max_sel = self._canvas_x_to_data(canvas, max(start_x, end_x), meta)
        if max_sel <= min_sel:
            return
        self.loot_selection_var.set(self.describe_loot_chart_selection(canvas, min_sel, max_sel))
        self.loot_zoom_ranges[canvas] = (min_sel, max_sel)
        self.redraw_all_loot_charts()

    def redraw_all_loot_charts(self):
        for canvas in (getattr(self, "loot_value_time_canvas", None), getattr(self, "loot_cost_canvas", None), getattr(self, "loot_items_time_canvas", None)):
            if canvas is not None:
                self.redraw_chart_canvas(canvas)

    def _canvas_x_to_data(self, canvas, px, meta):
        left = meta["left"]
        right = meta["right"]
        min_x = meta["min_x"]
        max_x = meta["max_x"]
        px = max(left, min(right, float(px)))
        if right <= left:
            return min_x
        return min_x + (max_x - min_x) * ((px - left) / (right - left))

    def _visible_x_bounds(self, min_x, max_x, canvas=None):
        zoom_range = self.loot_zoom_ranges.get(canvas) if canvas is not None else None
        if zoom_range:
            zoom_min, zoom_max = zoom_range
            clipped_min = max(float(min_x), float(zoom_min))
            clipped_max = min(float(max_x), float(zoom_max))
            if clipped_max > clipped_min:
                return clipped_min, clipped_max
        return float(min_x), float(max_x)

    def _points_in_x_range(self, points, min_x, max_x):
        return [point for point in list(points or []) if min_x <= float(point.get("x", 0.0) or 0.0) <= max_x]

    def _remember_chart_meta(self, canvas, *, left, top, right, bottom, min_x, max_x, kind):
        self.loot_chart_meta[canvas] = {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "min_x": float(min_x),
            "max_x": float(max_x),
            "kind": kind,
        }

    def _selection_time_text(self, min_x, max_x, x_is_time):
        if x_is_time:
            return f"{self._time_axis_label(min_x)} - {self._time_axis_label(max_x)} ({self._time_axis_label(max_x - min_x)})"
        return f"{min_x:.4f} - {max_x:.4f}"

    def describe_loot_chart_selection(self, canvas, min_x, max_x):
        payload = self.loot_chart_payloads.get(canvas) or {}
        kind = payload.get("kind")
        x_is_time = bool(payload.get("x_is_time", False))
        range_text = self._selection_time_text(min_x, max_x, x_is_time)
        if kind == "line":
            points = self._points_in_x_range(payload.get("points") or [], min_x, max_x)
            if not points:
                return f"Selected {range_text}: no return points."
            first = float(points[0].get("y", 0.0) or 0.0)
            last = float(points[-1].get("y", 0.0) or 0.0)
            return f"Selected {range_text}: {len(points)} plotted return points, return {first:.2f}% -> {last:.2f}%."
        if kind == "scatter":
            points = self._points_in_x_range(payload.get("points") or [], min_x, max_x)
            if not points:
                return f"Selected {range_text}: no loot events."
            total_cost = sum(float(point.get("x", 0.0) or 0.0) for point in points)
            total_loot = sum(float(point.get("y", 0.0) or 0.0) for point in points)
            avg_multi = (total_loot / total_cost) if total_cost else 0.0
            return f"Selected cost range {range_text}: {len(points)} loot events, total cost {total_cost:.4f} PED, total loot {total_loot:.4f} PED, average {avg_multi:.2f}x."
        if kind in ("multi_points", "multi_line"):
            totals = []
            for name, points in payload.get("series") or []:
                selected = self._points_in_x_range(points, min_x, max_x)
                total = sum(float(point.get("y", 0.0) or 0.0) for point in selected)
                if total > 0:
                    totals.append((name, total))
            totals.sort(key=lambda item: item[1], reverse=True)
            if not totals:
                return f"Selected {range_text}: no selected item drops."
            top = "; ".join(f"{name}: {total:.0f}" for name, total in totals[:8])
            return f"Selected {range_text}: item totals: {top}"
        return f"Selected {range_text}."

    def elapsed_minutes(self, base_time, event_time, fallback_index):
        if base_time is None or event_time is None:
            return float(fallback_index)
        return max(0.0, (event_time - base_time).total_seconds() / 60.0)

    def format_chart_time_label(self, event_time, fallback_index):
        if event_time is None:
            return str(fallback_index)
        return event_time.strftime("%H:%M:%S")

    def _chart_area(self, canvas):
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 170)
        # Use almost the full canvas width. Legends/reference labels are drawn
        # inside the plot area now, so we do not waste a large empty lane on
        # the right side of every chart.
        left, top, right, bottom = 58, 28, width - 28, height - 40
        return width, height, left, top, right, bottom

    def _format_axis_value(self, value, suffix=""):
        value = float(value)
        if suffix == "%":
            if abs(value) >= 100:
                text = f"{value:.0f}"
            elif abs(value) >= 10:
                text = f"{value:.1f}"
            else:
                text = f"{value:.2f}"
        elif abs(value) >= 100:
            text = f"{value:.0f}"
        elif abs(value) >= 10:
            text = f"{value:.1f}"
        elif abs(value) >= 1:
            text = f"{value:.2f}"
        else:
            text = f"{value:.4f}"
        if suffix:
            return f"{text}{suffix}"
        return text

    def _time_axis_label(self, minutes_value):
        minutes_value = float(minutes_value)
        total_seconds = int(round(minutes_value * 60.0))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _draw_xy_axes(self, canvas, title, x_label, y_label, min_x, max_x, min_y, max_y, *, x_is_time=False, y_suffix=""):
        width, height, left, top, right, bottom = self._chart_area(canvas)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#ffffff", outline="")
        axis_color = "#8a95a3"
        grid_color = "#e6e9ee"
        text_color = "#111827"
        muted_color = "#475467"

        canvas.create_text(width / 2, 14, text=title, fill=text_color, font=("Arial", 11, "bold"))
        canvas.create_line(left, bottom, right, bottom, fill=axis_color)
        canvas.create_line(left, top, left, bottom, fill=axis_color)

        x_range = max(max_x - min_x, 1e-9)
        y_range = max(max_y - min_y, 1e-9)

        for i in range(5):
            frac = i / 4
            y = bottom - (bottom - top) * frac
            y_value = min_y + y_range * frac
            canvas.create_line(left, y, right, y, fill=grid_color)
            canvas.create_text(left - 6, y, anchor="e", text=self._format_axis_value(y_value, y_suffix), fill=muted_color, font=("Arial", 8))

        for i in range(6):
            frac = i / 5
            x = left + (right - left) * frac
            x_value = min_x + x_range * frac
            canvas.create_line(x, top, x, bottom, fill=grid_color)
            label = self._time_axis_label(x_value) if x_is_time else self._format_axis_value(x_value)
            canvas.create_text(x, bottom + 14, anchor="n", text=label, fill=muted_color, font=("Arial", 8))

        canvas.create_text((left + right) / 2, height - 8, text=x_label, fill=text_color, font=("Arial", 9))
        canvas.create_text(14, (top + bottom) / 2, text=y_label, fill=text_color, font=("Arial", 9), angle=90)
        return width, height, left, top, right, bottom

    def _project_point(self, x, y, left, top, right, bottom, min_x, max_x, min_y, max_y):
        x_range = max(max_x - min_x, 1e-9)
        y_range = max(max_y - min_y, 1e-9)
        px = left + (right - left) * ((float(x) - min_x) / x_range)
        py = bottom - (bottom - top) * ((float(y) - min_y) / y_range)
        return px, py

    def render_line_chart(self, canvas, points, *, title, x_label, y_label, x_is_time=True, y_suffix="", smooth=False, y_reference_lines=None):
        payload = {
            "kind": "line",
            "points": list(points or []),
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "y_suffix": y_suffix,
            "smooth": bool(smooth),
            "y_reference_lines": list(y_reference_lines or []),
        }
        self.loot_chart_payloads[canvas] = payload
        self._draw_line_chart_payload(canvas, payload)

    def _draw_line_chart_payload(self, canvas, payload):
        all_points = list(payload.get("points") or [])
        if not all_points:
            self.clear_chart(canvas, "No loot data")
            return
        all_x_values = [float(point.get("x", 0.0) or 0.0) for point in all_points]
        full_min_x = 0.0 if bool(payload.get("x_is_time", True)) else min(all_x_values)
        full_max_x = max(all_x_values)
        if full_min_x == full_max_x:
            full_max_x = full_min_x + 1.0
        min_x, max_x = self._visible_x_bounds(full_min_x, full_max_x, canvas)
        points = self._points_in_x_range(all_points, min_x, max_x)
        if not points:
            self.clear_chart(canvas, "No data in selected zoom range")
            return
        y_values = [float(point.get("y", 0.0) or 0.0) for point in points]
        min_y = 0.0
        max_y = (max(y_values) * 1.10) if max(y_values) > 0 else 1.0
        reference_lines = list(payload.get("y_reference_lines") or [])
        for reference_value, _reference_label in reference_lines:
            try:
                max_y = max(max_y, float(reference_value) * 1.08)
            except (TypeError, ValueError):
                pass
        _, _, left, top, right, bottom = self._draw_xy_axes(
            canvas,
            payload.get("title", ""),
            payload.get("x_label", ""),
            payload.get("y_label", ""),
            min_x,
            max_x,
            min_y,
            max_y,
            x_is_time=bool(payload.get("x_is_time", True)),
            y_suffix=str(payload.get("y_suffix", "") or ""),
        )
        self._remember_chart_meta(canvas, left=left, top=top, right=right, bottom=bottom, min_x=min_x, max_x=max_x, kind="line")
        for reference_value, reference_label in reference_lines:
            try:
                reference_value = float(reference_value)
            except (TypeError, ValueError):
                continue
            if min_y <= reference_value <= max_y:
                x1, y1 = self._project_point(min_x, reference_value, left, top, right, bottom, min_x, max_x, min_y, max_y)
                x2, y2 = self._project_point(max_x, reference_value, left, top, right, bottom, min_x, max_x, min_y, max_y)
                canvas.create_line(x1, y1, x2, y2, fill="#16a34a", dash=(5, 4), width=2)
                label_x = max(left + 36, min(right - 6, x2 - 6))
                label_y = max(top + 10, min(bottom - 10, y2 - 8))
                canvas.create_text(label_x, label_y, anchor="e", text=str(reference_label), fill="#15803d", font=("Arial", 9, "bold"))
        coords = []
        for point in points:
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            coords.extend([px, py])
        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#2563eb", width=2, smooth=bool(payload.get("smooth", False)))
        # Drawing an oval for every point is expensive with long sessions.
        dot_stride = max(1, len(points) // 250)
        for idx, point in enumerate(points):
            if idx % dot_stride != 0 and idx != len(points) - 1:
                continue
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#2563eb", outline="")

    def render_scatter_chart(self, canvas, points, *, title, x_label, y_label, x_is_time=False, y_suffix="", draw_break_even=False, draw_multiplier_lines=False):
        payload = {
            "kind": "scatter",
            "points": list(points or []),
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "y_suffix": y_suffix,
            "draw_break_even": bool(draw_break_even),
            "draw_multiplier_lines": bool(draw_multiplier_lines),
        }
        self.loot_chart_payloads[canvas] = payload
        self._draw_scatter_chart_payload(canvas, payload)

    def _draw_scatter_chart_payload(self, canvas, payload):
        all_points = list(payload.get("points") or [])
        if not all_points:
            self.clear_chart(canvas, "No loot data")
            return
        all_x_values = [float(point.get("x", 0.0) or 0.0) for point in all_points]
        full_min_x = 0.0
        full_max_x = (max(all_x_values) * 1.10) if max(all_x_values) > 0 else 1.0
        min_x, max_x = self._visible_x_bounds(full_min_x, full_max_x, canvas)
        points = self._points_in_x_range(all_points, min_x, max_x)
        if not points:
            self.clear_chart(canvas, "No data in selected zoom range")
            return
        x_values = [float(point.get("x", 0.0) or 0.0) for point in points]
        y_values = [float(point.get("y", 0.0) or 0.0) for point in points]
        min_y = 0.0
        max_y = (max(y_values) * 1.10) if max(y_values) > 0 else 1.0

        positive_costs = sorted(float(point.get("x", 0.0) or 0.0) for point in points if float(point.get("x", 0.0) or 0.0) > 0)
        reference_cost = positive_costs[len(positive_costs) // 2] if positive_costs else 0.0
        multiplier_values = []
        if payload.get("draw_break_even"):
            multiplier_values.append(1.0)
        if payload.get("draw_multiplier_lines"):
            observed_max_multiplier = 0.0
            for point in points:
                x = float(point.get("x", 0.0) or 0.0)
                y = float(point.get("y", 0.0) or 0.0)
                if x > 0:
                    observed_max_multiplier = max(observed_max_multiplier, y / x)
            for multiplier in (2.0, 5.0, 10.0, 20.0, 50.0, 100.0):
                if observed_max_multiplier >= multiplier * 0.85:
                    multiplier_values.append(multiplier)
        if reference_cost > 0:
            for multiplier in multiplier_values:
                max_y = max(max_y, reference_cost * multiplier * 1.08)

        _, _, left, top, right, bottom = self._draw_xy_axes(
            canvas,
            payload.get("title", ""),
            payload.get("x_label", ""),
            payload.get("y_label", ""),
            min_x,
            max_x,
            min_y,
            max_y,
            x_is_time=bool(payload.get("x_is_time", False)),
            y_suffix=str(payload.get("y_suffix", "") or ""),
        )
        self._remember_chart_meta(canvas, left=left, top=top, right=right, bottom=bottom, min_x=min_x, max_x=max_x, kind="scatter")
        # Horizontal multiplier guides use the median visible cost as the reference cost.
        # This keeps the guides parallel to the X axis while still showing where x1/x5/x10
        # loot values are for the common kill cost cluster.
        if reference_cost > 0:
            for multiplier in multiplier_values:
                if multiplier <= 0:
                    continue
                y_value = reference_cost * multiplier
                if not (min_y <= y_value <= max_y):
                    continue
                x1, y1 = self._project_point(min_x, y_value, left, top, right, bottom, min_x, max_x, min_y, max_y)
                x2, y2 = self._project_point(max_x, y_value, left, top, right, bottom, min_x, max_x, min_y, max_y)
                width = 2 if multiplier == 1.0 else 1
                canvas.create_line(x1, y1, x2, y2, fill="#16a34a", dash=(5, 4), width=width)
                label = "1.0x" if multiplier == 1.0 else f"x{multiplier:g}"
                label_y = max(top + 10, min(bottom - 10, y2 - 7))
                canvas.create_text(right - 6, label_y, anchor="e", text=label, fill="#15803d", font=("Arial", 9, "bold"))
        for point in points:
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill="#2563eb", outline="")

    def _draw_chart_legend(self, canvas, entries, *, right, top, bottom):
        entries = [(name, color) for name, color in entries if name]
        if not entries:
            return
        visible_entries = entries[:10]
        line_height = 16
        max_text_width = 0
        for name, _color in visible_entries:
            max_text_width = max(max_text_width, min(165, max(70, len(str(name)[:24]) * 7)))

        # Draw the legend inside the plotting area, in the top-right corner.
        # This avoids both problems: no overlap with axis tick values, and no
        # large unused empty space to the right of charts that have no legend.
        box_right = right - 8
        box_left = max(64, box_right - max_text_width - 34)
        box_top = top + 8
        box_bottom = min(bottom - 8, box_top + 8 + len(visible_entries) * line_height)
        canvas.create_rectangle(box_left, box_top, box_right, box_bottom, fill="#ffffff", outline="#94a3b8")
        y = box_top + 12
        for name, color in visible_entries:
            if y > box_bottom - 4:
                break
            text = str(name)[:24]
            canvas.create_line(box_left + 8, y, box_left + 22, y, fill=color, width=3)
            canvas.create_text(box_left + 28, y, anchor="w", text=text, fill="#111827", font=("Arial", 9, "bold"))
            y += line_height

    def render_multi_line_chart(self, canvas, series, *, title, x_label, y_label, x_is_time=True, y_suffix="", smooth=False):
        payload = {
            "kind": "multi_line",
            "series": [(name, list(points or [])) for name, points in list(series or [])],
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "y_suffix": y_suffix,
            "smooth": bool(smooth),
        }
        self.loot_chart_payloads[canvas] = payload
        self._draw_multi_line_chart_payload(canvas, payload)

    def _draw_multi_line_chart_payload(self, canvas, payload):
        series = list(payload.get("series") or [])
        if not series:
            self.clear_chart(canvas, "No selected item data")
            return
        all_points = [point for _, points in series for point in points]
        if not all_points:
            self.clear_chart(canvas, "No selected item data")
            return
        x_values = [float(point.get("x", 0.0) or 0.0) for point in all_points]
        y_values = [float(point.get("y", 0.0) or 0.0) for point in all_points]
        min_x = 0.0 if bool(payload.get("x_is_time", True)) else min(x_values)
        max_x = max(x_values) if x_values else 1.0
        if min_x == max_x:
            max_x = min_x + 1.0
        min_y = 0.0
        max_y = (max(y_values) * 1.10) if max(y_values) > 0 else 1.0
        _, _, left, top, right, bottom = self._draw_xy_axes(
            canvas,
            payload.get("title", ""),
            payload.get("x_label", ""),
            payload.get("y_label", ""),
            min_x,
            max_x,
            min_y,
            max_y,
            x_is_time=bool(payload.get("x_is_time", True)),
            y_suffix=str(payload.get("y_suffix", "") or ""),
        )
        palette = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#65a30d", "#be123c", "#0284c7", "#ca8a04", "#475569", "#7c3aed"]
        legend_entries = []
        for index, (name, points) in enumerate(series):
            if not points:
                continue
            color = palette[index % len(palette)]
            legend_entries.append((name, color))
            coords = []
            for point in points:
                px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
                coords.extend([px, py])
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=color, width=2, smooth=bool(payload.get("smooth", False)))
            for point in points[-1:]:
                px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
                canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")
        self._draw_chart_legend(canvas, legend_entries, right=right, top=top, bottom=bottom)

    def render_multi_point_chart(self, canvas, series, *, title, x_label, y_label, x_is_time=True):
        payload = {
            "kind": "multi_points",
            "series": [(name, list(points or [])) for name, points in list(series or [])],
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
        }
        self.loot_chart_payloads[canvas] = payload
        self._draw_multi_point_chart_payload(canvas, payload)

    def _draw_multi_point_chart_payload(self, canvas, payload):
        series = list(payload.get("series") or [])
        if not series:
            self.clear_chart(canvas, "No selected item data")
            return
        all_points = [point for _, points in series for point in points]
        if not all_points:
            self.clear_chart(canvas, "No selected item data")
            return
        x_values = [float(point.get("x", 0.0) or 0.0) for point in all_points]
        full_min_x = 0.0 if bool(payload.get("x_is_time", True)) else min(x_values)
        full_max_x = max(x_values) if x_values else 1.0
        if full_min_x == full_max_x:
            full_max_x = full_min_x + 1.0
        min_x, max_x = self._visible_x_bounds(full_min_x, full_max_x, canvas)
        visible_series = []
        visible_points_all = []
        for name, points in series:
            selected_points = self._points_in_x_range(points, min_x, max_x)
            if selected_points:
                visible_series.append((name, selected_points))
                visible_points_all.extend(selected_points)
        if not visible_points_all:
            self.clear_chart(canvas, "No selected item data in zoom range")
            return
        y_values = [float(point.get("y", 0.0) or 0.0) for point in visible_points_all]
        min_y = 0.0
        max_y = (max(y_values) * 1.15) if max(y_values) > 0 else 1.0
        _, _, left, top, right, bottom = self._draw_xy_axes(
            canvas,
            payload.get("title", ""),
            payload.get("x_label", ""),
            payload.get("y_label", ""),
            min_x,
            max_x,
            min_y,
            max_y,
            x_is_time=bool(payload.get("x_is_time", True)),
            y_suffix="",
        )
        self._remember_chart_meta(canvas, left=left, top=top, right=right, bottom=bottom, min_x=min_x, max_x=max_x, kind="multi_points")
        palette = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#65a30d", "#be123c", "#0284c7", "#ca8a04", "#475569", "#7c3aed"]
        legend_entries = []
        for index, (name, points) in enumerate(visible_series):
            if not points:
                continue
            color = palette[index % len(palette)]
            legend_entries.append((name, color))
            # Draw per-event quantity as stems/dots instead of cumulative lines.
            for point in points:
                px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
                _, zero_y = self._project_point(point.get("x", 0.0), 0.0, left, top, right, bottom, min_x, max_x, min_y, max_y)
                canvas.create_line(px, zero_y, px, py, fill=color, width=1)
                canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline="")
        self._draw_chart_legend(canvas, legend_entries, right=right, top=top, bottom=bottom)

    def create_hunting_tab(self):
        profiles = ttk.LabelFrame(self.hunting_tab, text="Saved hunting setups", padding=10)
        profiles.pack(fill="x", padx=12, pady=(12, 0))

        ttk.Label(profiles, text="Setup name:").grid(row=0, column=0, sticky="w")
        self.hunting_setup_combo = ttk.Combobox(
            profiles,
            textvariable=self.hunting_setup_name_var,
            values=sorted(self.hunting_setups.keys(), key=str.lower),
            width=48,
        )
        self.hunting_setup_combo.grid(row=0, column=1, sticky="ew", padx=8)
        self.hunting_setup_combo.bind("<<ComboboxSelected>>", self.load_named_hunting_setup)
        ttk.Button(profiles, text="Load", command=self.load_named_hunting_setup).grid(row=0, column=2, padx=4)
        ttk.Button(profiles, text="Save / Update", command=self.save_named_hunting_setup).grid(row=0, column=3, padx=4)
        ttk.Button(profiles, text="Delete", command=self.delete_named_hunting_setup).grid(row=0, column=4, padx=4)
        ttk.Label(profiles, textvariable=self.hunting_setup_status_var).grid(row=1, column=1, columnspan=4, sticky="w", padx=8, pady=(5, 0))
        profiles.columnconfigure(1, weight=1)

        frame = ttk.Frame(self.hunting_tab, padding=12)
        frame.pack(fill="x")

        ttk.Checkbutton(frame, text="Count hunting / PED cycled during sync", variable=self.count_hunting_var, command=self.on_hunting_changed).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Weapon search:").grid(row=1, column=0, sticky="w")
        weapon_search = ttk.Entry(frame, textvariable=self.weapon_filter_var, width=35)
        weapon_search.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        weapon_search.bind("<KeyRelease>", lambda e: self.filter_weapon_values())
        ttk.Button(frame, text="Clear", command=self.clear_weapon_filter).grid(row=1, column=2, sticky="w", padx=4)

        ttk.Label(frame, text="Weapon:").grid(row=2, column=0, sticky="w")
        self.weapon_combo = ttk.Combobox(frame, textvariable=self.weapon_var, values=self.all_weapon_names, width=70)
        self.weapon_combo.grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        self.weapon_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.weapon_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        self.weapon_combo.bind("<KeyRelease>", lambda e: self.filter_weapon_values(self.weapon_var.get()))
        ttk.Label(frame, textvariable=self.weapon_cost_var).grid(row=2, column=2, sticky="w")

        ttk.Label(frame, text="Amplifier search:").grid(row=3, column=0, sticky="w")
        amplifier_search = ttk.Entry(frame, textvariable=self.amplifier_filter_var, width=35)
        amplifier_search.grid(row=3, column=1, sticky="ew", padx=8, pady=4)
        amplifier_search.bind("<KeyRelease>", lambda e: self.filter_amplifier_values())
        ttk.Button(frame, text="Clear", command=self.clear_amplifier_filter).grid(row=3, column=2, sticky="w", padx=4)

        ttk.Label(frame, text="Amplifier:").grid(row=4, column=0, sticky="w")
        self.amplifier_combo = ttk.Combobox(frame, textvariable=self.amplifier_var, values=[""] + self.all_amplifier_names, width=70)
        self.amplifier_combo.grid(row=4, column=1, sticky="ew", padx=8, pady=4)
        self.amplifier_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.amplifier_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        self.amplifier_combo.bind("<KeyRelease>", lambda e: self.filter_amplifier_values(self.amplifier_var.get()))

        ttk.Label(frame, text="Attachment search:").grid(row=5, column=0, sticky="w")
        attachment_search = ttk.Entry(frame, textvariable=self.attachment_filter_var, width=35)
        attachment_search.grid(row=5, column=1, sticky="ew", padx=8, pady=4)
        attachment_search.bind("<KeyRelease>", lambda e: self.filter_attachment_values())
        ttk.Button(frame, text="Clear", command=self.clear_attachment_filter).grid(row=5, column=2, sticky="w", padx=4)

        self.attachment_combos = []
        for index, attachment_var in enumerate(self.attachment_vars, start=1):
            row = 5 + index
            ttk.Label(frame, text=f"Attachment {index}:").grid(row=row, column=0, sticky="w")
            combo = ttk.Combobox(frame, textvariable=attachment_var, values=[""] + self.all_attachment_names, width=70)
            combo.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
            combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
            combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
            combo.bind("<KeyRelease>", lambda e, var=attachment_var: self.filter_attachment_values(var.get()))
            self.attachment_combos.append(combo)

        ttk.Label(frame, text="Mob search:").grid(row=9, column=0, sticky="w")
        mob_search = ttk.Entry(frame, textvariable=self.mob_filter_var, width=35)
        mob_search.grid(row=9, column=1, sticky="ew", padx=8, pady=4)
        mob_search.bind("<KeyRelease>", lambda e: self.filter_mob_values())
        ttk.Button(frame, text="Clear", command=self.clear_mob_filter).grid(row=9, column=2, sticky="w", padx=4)

        ttk.Label(frame, text="Mob:").grid(row=10, column=0, sticky="w")
        self.mob_combo = ttk.Combobox(frame, textvariable=self.mob_var, values=self.all_mob_names, width=70)
        self.mob_combo.grid(row=10, column=1, sticky="ew", padx=8, pady=4)
        self.mob_combo.bind("<<ComboboxSelected>>", lambda e: self.on_mob_changed())
        self.mob_combo.bind("<FocusOut>", lambda e: self.on_mob_changed())
        self.mob_combo.bind("<KeyRelease>", lambda e: self.filter_mob_values(self.mob_var.get()))

        ttk.Label(frame, text="Maturity:").grid(row=11, column=0, sticky="w")
        self.maturity_combo = ttk.Combobox(frame, textvariable=self.maturity_var, values=[], width=70)
        self.maturity_combo.grid(row=11, column=1, sticky="ew", padx=8, pady=4)
        self.maturity_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.maturity_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        ttk.Label(frame, textvariable=self.mob_info_var).grid(row=11, column=2, sticky="w")

        ttk.Label(frame, text="The current selection is saved automatically. Use Saved hunting setups above for named profiles.").grid(row=12, column=1, sticky="w", padx=8, pady=12)
        frame.columnconfigure(1, weight=1)
        self.update_maturity_values()

        help_box = ttk.LabelFrame(self.hunting_tab, text="Counting rule", padding=10)
        help_box.pack(fill="x", padx=12, pady=10)
        ttk.Label(
            help_box,
            text=(
                "PED cycled increments on every player attack: normal hit, critical hit, target Jammed/Evaded/Dodged, or 'You missed'.\n"
                "Jammed, Evaded, and Dodged are one target-defense category; 'You missed' is counted separately.\n"
                "It ignores enemy attacks like 'The attack missed you' and 'You took damage'.\n"
                "Per attack cost is calculated from weapon, amplifier, and attachment decay / 100 + ammo_burn / 10000 PED."
            ),
            justify="left",
        ).pack(anchor="w")

    def create_sessions_tab(self):
        top = ttk.Frame(self.sessions_tab, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh", command=self.refresh_sessions_table).pack(side="left")
        ttk.Button(top, text="Load Current Skills from Selected Session", command=self.load_current_skills_from_selected_session).pack(side="left", padx=6)
        ttk.Button(top, text="Add Selected to Mob Analysis", command=self.add_selected_sessions_to_analysis).pack(side="left", padx=6)
        ttk.Button(top, text="Remove Selected from Mob Analysis", command=self.remove_selected_sessions_from_analysis).pack(side="left", padx=6)
        ttk.Button(top, text="Delete Selected Sessions", command=self.delete_selected_sessions).pack(side="left", padx=6)
        ttk.Button(top, text="Clear Sessions", command=self.clear_sessions).pack(side="left", padx=6)

        columns = (
            "started", "ended", "weapon", "mob", "attacks", "defended", "misses", "damage", "ped",
            "dpp", "efficiency", "ped_h", "loot", "loot_percent", "loot_events", "cost_per_kill", "skill_tt",
            "skill_tt_percent", "avg_skill_tt_per_hour", "avg_ped_loss_100",
            "skill_tt_minus_avg_loss_100", "skill_points", "skill_events", "skills",
        )
        self.sessions_tree = ttk.Treeview(
            self.sessions_tab,
            columns=columns,
            show="headings",
            height=22,
            selectmode="extended",
        )
        setup = [
            ("started", "Started", 155), ("ended", "Ended", 155), ("weapon", "Weapon / Amp", 260),
            ("mob", "Mob", 170), ("attacks", "Attacks", 75),
            ("defended", "J/E/D", 70), ("misses", "Misses", 70), ("damage", "Damage", 85),
            ("ped", "PED cycled", 95), ("dpp", "DPP", 70), ("efficiency", "Efficiency", 80),
            ("ped_h", "PED/h", 80), ("loot", "Loot PED", 85),
            ("loot_percent", "Loot %", 75),
            ("loot_events", "Loot events", 90),
            ("cost_per_kill", "Cost/kill", 90),
            ("skill_tt", "TT-equiv total", 105), ("skill_points", "Point total", 115),
            ("skill_tt_percent", "Skill TT %", 85),
            ("avg_skill_tt_per_hour", "Avg skill TT/h", 105),
            ("avg_ped_loss_100", "Avg PED lose/100", 125),
            ("skill_tt_minus_avg_loss_100", "Skill TT - avg lose/100", 145),
            ("skill_events", "Skill gains", 90), ("skills", "Skills gained", 300),
        ]
        for col, title, width in setup:
            self.sessions_tree.heading(col, text=title)
            self.sessions_tree.column(col, width=width, anchor="center" if col not in ("weapon", "mob", "skills") else "w")
        self.make_tree_sortable(self.sessions_tree, {col: title for col, title, _ in setup})
        sessions_xscroll = ttk.Scrollbar(self.sessions_tab, orient="horizontal", command=self.sessions_tree.xview)
        self.sessions_tree.configure(xscrollcommand=sessions_xscroll.set)
        self.sessions_tree.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        sessions_xscroll.pack(fill="x", padx=10, pady=(0, 10))
        self.sessions_tree.tag_configure("analysis_valid", background="#dff3df")
        self.sessions_tree.bind("<<TreeviewSelect>>", self.on_session_selected)

    def add_selected_sessions_to_analysis(self):
        indices = self.selected_session_indices_from_table()
        if not indices:
            messagebox.showwarning("No sessions selected", "Select one or more valid sessions first.")
            return

        existing_by_id = {
            str(session.get("id", "")): index
            for index, session in enumerate(self.analysis_sessions)
            if isinstance(session, dict) and session.get("id")
        }
        added = 0
        updated = 0
        skipped = 0
        for index in indices:
            session = self.sessions[index]
            if not isinstance(session, dict):
                skipped += 1
                continue
            mob_name = str(session.get("mob", "") or "").strip()
            ped_cycled = parse_float(session.get("ped_cycled"), 0.0)
            if not mob_name or ped_cycled <= 0:
                skipped += 1
                continue
            copied = json.loads(json.dumps(session, ensure_ascii=False))
            session_id = str(copied.get("id", "") or "")
            if session_id and session_id in existing_by_id:
                self.analysis_sessions[existing_by_id[session_id]] = copied
                updated += 1
            else:
                self.analysis_sessions.append(copied)
                if session_id:
                    existing_by_id[session_id] = len(self.analysis_sessions) - 1
                added += 1

        save_json(ANALYSIS_SESSIONS_FILE, self.analysis_sessions)
        self.mob_analysis_status_var.set(
            f"Analysis sessions: {len(self.analysis_sessions)} | added {added}, updated {updated}, skipped {skipped}."
        )
        self.refresh_sessions_table()
        self.refresh_mob_analysis()
        messagebox.showinfo(
            "Mob analysis sessions saved",
            f"Saved to {ANALYSIS_SESSIONS_FILE}.\n\nAdded: {added}\nUpdated: {updated}\nSkipped: {skipped}",
        )

    def remove_selected_sessions_from_analysis(self):
        indices = self.selected_session_indices_from_table()
        if not indices:
            messagebox.showwarning("No sessions selected", "Select one or more sessions first.")
            return
        selected_ids = {
            str(self.sessions[index].get("id", "") or "")
            for index in indices
            if isinstance(self.sessions[index], dict)
        }
        selected_ids.discard("")
        if not selected_ids:
            messagebox.showwarning("Missing session IDs", "The selected sessions cannot be matched to the analysis file.")
            return
        before = len(self.analysis_sessions)
        self.analysis_sessions = [
            session for session in self.analysis_sessions
            if not isinstance(session, dict) or str(session.get("id", "") or "") not in selected_ids
        ]
        removed = before - len(self.analysis_sessions)
        save_json(ANALYSIS_SESSIONS_FILE, self.analysis_sessions)
        self.mob_analysis_status_var.set(f"Removed {removed} session(s). Valid analysis sessions: {len(self.analysis_sessions)}.")
        self.refresh_sessions_table()
        self.refresh_mob_analysis()

    def current_profession_level(self, profession_name: str) -> float:
        """Calculate a profession level from current skills and attributes.

        Entropia attributes use an x20 contribution in profession formulas.
        Normal skills use their stored value directly. The resulting weighted
        value remains on the tracker's 100x profession scale and is normalized
        by ``calculated_looter_levels`` before being displayed.
        """
        profession = PROFESSIONS.get(profession_name) or {}
        skills = profession.get("skills", {}) or {}
        total = 0.0
        for skill_name, weight in skills.items():
            skill_value = profession_weighted_value(
                skill_name,
                self.current_skills.get(skill_name, 0.0),
            )
            total += skill_value * parse_float(weight, 0.0) / 100.0
        return total

    def calculated_looter_levels(self) -> dict:
        """Return normalized looter profession levels on the 0-100 scale.

        Entropia skill points are stored in the tracker on a 100x scale for
        profession calculations, so a calculated value such as 4913 means
        profession level 49.13.
        """
        return {
            "Animal": self.current_profession_level("Animal Looter") / 100.0,
            "Robot": self.current_profession_level("Robot Looter") / 100.0,
            "Mutant": self.current_profession_level("Mutant Looter") / 100.0,
        }

    def load_analysis_looters_from_current_skills(self):
        for looter_type, level in self.calculated_looter_levels().items():
            variable = self.analysis_looter_vars.get(looter_type)
            if variable is not None:
                variable.set(f"{level:.4f}")

    def use_current_skill_looters(self):
        self.load_analysis_looters_from_current_skills()
        self.refresh_mob_analysis()

    def create_mob_analysis_tab(self):
        inputs = ttk.LabelFrame(self.mob_analysis_tab, text="Current character values", padding=10)
        inputs.pack(fill="x", padx=10, pady=10)

        ttk.Label(inputs, text="Efficiency (0-100):").grid(row=0, column=0, sticky="w")
        ttk.Entry(inputs, textvariable=self.analysis_efficiency_var, width=10).grid(row=0, column=1, sticky="w", padx=(6, 18))
        for column, looter_type in enumerate(("Animal", "Robot", "Mutant"), start=2):
            ttk.Label(inputs, text=f"{looter_type} Looter (0-100):").grid(row=0, column=column * 2 - 2, sticky="w")
            ttk.Entry(inputs, textvariable=self.analysis_looter_vars[looter_type], width=10).grid(
                row=0, column=column * 2 - 1, sticky="w", padx=(6, 18)
            )
        ttk.Button(inputs, text="Calculate", command=self.refresh_mob_analysis).grid(row=0, column=8, padx=6)
        ttk.Button(inputs, text="Use current skill levels", command=self.use_current_skill_looters).grid(row=0, column=9, padx=6)
        ttk.Button(inputs, text="Reload valid sessions", command=self.reload_analysis_sessions).grid(row=0, column=10, padx=6)
        ttk.Label(
            inputs,
            text=(
                "Expected TT return = 86% + 7 × Efficiency / 100 + 7 × matching Looter / 100. "
                "Expected return after MU = expected TT return × historical loot MU multiplier."
            ),
        ).grid(row=1, column=0, columnspan=11, sticky="w", pady=(8, 0))

        status = ttk.LabelFrame(self.mob_analysis_tab, text="Analysis status", padding=8)
        status.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(status, textvariable=self.mob_analysis_status_var, justify="left").pack(anchor="w")

        columns = (
            "mob", "type", "looter", "sessions", "ped", "tt_loot", "observed_tt",
            "historical_mu", "expected_tt", "expected_after_mu", "sample_items",
        )
        self.mob_analysis_tree = ttk.Treeview(
            self.mob_analysis_tab, columns=columns, show="headings", height=23
        )
        setup = [
            ("mob", "Mob", 230), ("type", "Type", 75), ("looter", "Looter", 75),
            ("sessions", "Sessions", 75), ("ped", "PED cycled", 100),
            ("tt_loot", "TT loot", 90), ("observed_tt", "Observed TT %", 105),
            ("historical_mu", "Historical MU %", 115), ("expected_tt", "Expected TT %", 105),
            ("expected_after_mu", "Expected after MU %", 140), ("sample_items", "Loot items", 90),
        ]
        for column, title, width in setup:
            self.mob_analysis_tree.heading(column, text=title)
            self.mob_analysis_tree.column(column, width=width, anchor="w" if column == "mob" else "center")
        self.make_tree_sortable(self.mob_analysis_tree, {column: title for column, title, _ in setup})
        xscroll = ttk.Scrollbar(self.mob_analysis_tab, orient="horizontal", command=self.mob_analysis_tree.xview)
        yscroll = ttk.Scrollbar(self.mob_analysis_tab, orient="vertical", command=self.mob_analysis_tree.yview)
        self.mob_analysis_tree.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self.mob_analysis_tree.pack(fill="both", expand=True, padx=10)
        xscroll.pack(fill="x", padx=10)
        yscroll.place_forget()
        self.mob_analysis_tree.bind("<Double-1>", self.open_mob_analysis_details)

    def reload_analysis_sessions(self):
        loaded = load_json(ANALYSIS_SESSIONS_FILE, [])
        self.analysis_sessions = loaded if isinstance(loaded, list) else []
        self.refresh_mob_analysis()

    def analysis_number(self, variable, label):
        value = parse_float(variable.get(), None)
        if value is None or not 0.0 <= value <= 100.0:
            raise ValueError(f"{label} must be a number from 0 to 100.")
        return float(value)

    def mob_type_for_name(self, mob_name: str) -> str:
        mob = MOBS.get(mob_name) or {}
        raw_type = str(mob.get("type", "") or "").strip().casefold()
        if "robot" in raw_type:
            return "Robot"
        if "mutant" in raw_type:
            return "Mutant"
        return "Animal"

    def build_mob_analysis_results(self, efficiency: float, looter_levels: dict):
        self.reload_market_data_if_changed()
        grouped = {}
        for session in self.analysis_sessions:
            if not isinstance(session, dict):
                continue
            mob_name = str(session.get("mob", "") or "").strip()
            if not mob_name or mob_name not in MOBS:
                continue
            ped_cycled = max(0.0, parse_float(session.get("ped_cycled"), 0.0))
            if ped_cycled <= 0:
                continue
            loot_events = self.loot_events_for_session(session)
            item_totals = self.loot_item_value_totals(loot_events)
            tt_loot = sum(float(row.get("value_ped", 0.0) or 0.0) for row in item_totals.values())
            after_mu = sum(
                self.loot_value_after_markup(
                    item_name,
                    float(row.get("value_ped", 0.0) or 0.0),
                    int(row.get("quantity", 0) or 0),
                )
                for item_name, row in item_totals.items()
            )
            row = grouped.setdefault(mob_name, {
                "mob": mob_name,
                "type": self.mob_type_for_name(mob_name),
                "sessions": 0,
                "ped_cycled": 0.0,
                "tt_loot": 0.0,
                "after_mu": 0.0,
                "loot_events": 0,
                "items": {},
                "maturities": set(),
                "session_rows": [],
            })
            row["sessions"] += 1
            row["ped_cycled"] += ped_cycled
            row["tt_loot"] += tt_loot
            row["after_mu"] += after_mu
            row["loot_events"] += len(loot_events)
            maturity = str(session.get("maturity", "") or "").strip()
            if maturity:
                row["maturities"].add(maturity)
            for item_name, item_row in item_totals.items():
                total = row["items"].setdefault(item_name, {"quantity": 0, "value_ped": 0.0, "after_mu": 0.0})
                quantity = int(item_row.get("quantity", 0) or 0)
                value_ped = float(item_row.get("value_ped", 0.0) or 0.0)
                total["quantity"] += quantity
                total["value_ped"] += value_ped
                total["after_mu"] += self.loot_value_after_markup(item_name, value_ped, quantity)
            weapon = str(session.get("weapon", "") or "").strip()
            amplifier = str(session.get("amplifier", "") or "").strip()
            attachments = [
                str(name).strip()
                for name in list(session.get("attachments", []) or [])
                if str(name).strip()
            ]
            row["session_rows"].append({
                "id": session.get("id", ""),
                "started_at": session.get("started_at", ""),
                "maturity": maturity,
                "weapon": weapon,
                "amplifier": amplifier,
                "attachments": attachments,
                "ped_cycled": ped_cycled,
                "tt_loot": tt_loot,
                "after_mu": after_mu,
                "loot_events": len(loot_events),
            })

        results = []
        for row in grouped.values():
            mob_type = row["type"]
            looter = float(looter_levels.get(mob_type, 0.0))
            expected_tt = 86.0 + (7.0 * efficiency / 100.0) + (7.0 * looter / 100.0)
            historical_mu = percent(row["after_mu"], row["tt_loot"]) if row["tt_loot"] else 100.0
            expected_after_mu = expected_tt * historical_mu / 100.0
            row.update({
                "looter": looter,
                "expected_tt": expected_tt,
                "historical_mu": historical_mu,
                "expected_after_mu": expected_after_mu,
                "observed_tt": percent(row["tt_loot"], row["ped_cycled"]),
                "observed_after_mu": percent(row["after_mu"], row["ped_cycled"]),
            })
            results.append(row)
        return sorted(results, key=lambda row: (-row["expected_after_mu"], -row["ped_cycled"], row["mob"].casefold()))

    def refresh_mob_analysis(self):
        if not hasattr(self, "mob_analysis_tree"):
            return
        try:
            efficiency = self.analysis_number(self.analysis_efficiency_var, "Efficiency")
            looter_levels = {
                name: self.analysis_number(variable, f"{name} Looter")
                for name, variable in self.analysis_looter_vars.items()
            }
        except ValueError as exc:
            messagebox.showerror("Invalid analysis value", str(exc))
            return

        self.mob_analysis_results = self.build_mob_analysis_results(efficiency, looter_levels)
        self.mob_analysis_tree.delete(*self.mob_analysis_tree.get_children())
        self.mob_analysis_iid_to_result = {}
        for index, result in enumerate(self.mob_analysis_results):
            iid = f"mob_analysis_{index}"
            self.mob_analysis_iid_to_result[iid] = result
            self.mob_analysis_tree.insert("", "end", iid=iid, values=(
                result["mob"], result["type"], f'{result["looter"]:.2f}', result["sessions"],
                f'{result["ped_cycled"]:.2f}', f'{result["tt_loot"]:.2f}', f'{result["observed_tt"]:.2f}%',
                f'{result["historical_mu"]:.2f}%', f'{result["expected_tt"]:.2f}%',
                f'{result["expected_after_mu"]:.2f}%', len(result["items"]),
            ))
        self.apply_tree_sort(self.mob_analysis_tree)
        self.mob_analysis_status_var.set(
            f"Valid sessions: {len(self.analysis_sessions)} | mobs with usable data: {len(self.mob_analysis_results)} | "
            f"manual loot MU overrides weekly market MU; missing MU defaults to 100%."
        )
        self.save_state()

    def open_mob_analysis_details(self, event=None):
        tree = self.mob_analysis_tree
        iid = tree.identify_row(event.y) if event is not None else ""
        if not iid:
            selected = tree.selection()
            iid = selected[0] if selected else ""
        result = self.mob_analysis_iid_to_result.get(iid)
        if not result:
            return

        window = tk.Toplevel(self.root)
        window.title(f'{result["mob"]} analysis details')
        window.geometry("1100x700")
        window.minsize(850, 520)
        window.transient(self.root)

        summary = ttk.LabelFrame(window, text="Mob summary", padding=10)
        summary.pack(fill="x", padx=10, pady=10)
        ttk.Label(summary, text=(
            f'Mob: {result["mob"]} | Type: {result["type"]} | Matching looter: {result["looter"]:.2f}\n'
            f'Sessions: {result["sessions"]} | Maturities: {", ".join(sorted(result["maturities"])) or "-"} | '
            f'PED cycled: {result["ped_cycled"]:.4f} | Loot events: {result["loot_events"]}\n'
            f'Observed TT return: {result["observed_tt"]:.2f}% | Observed after MU: {result["observed_after_mu"]:.2f}% | '
            f'Historical loot MU: {result["historical_mu"]:.2f}%\n'
            f'Expected TT return: {result["expected_tt"]:.2f}% | Expected return after MU: {result["expected_after_mu"]:.2f}%'
        ), justify="left").pack(anchor="w")

        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        items_tab = ttk.Frame(notebook)
        sessions_tab = ttk.Frame(notebook)
        notebook.add(items_tab, text="Loot items")
        notebook.add(sessions_tab, text="Included sessions")

        item_columns = ("item", "quantity", "tt", "mu", "after_mu", "share")
        item_tree = ttk.Treeview(items_tab, columns=item_columns, show="headings")
        for column, title, width in [
            ("item", "Item", 330), ("quantity", "Quantity", 90), ("tt", "TT value", 110),
            ("mu", "MU source/value", 170), ("after_mu", "After MU", 110), ("share", "% of TT loot", 110),
        ]:
            item_tree.heading(column, text=title)
            item_tree.column(column, width=width, anchor="w" if column in ("item", "mu") else "center")
        item_tree.pack(fill="both", expand=True)
        for item_name, row in sorted(result["items"].items(), key=lambda pair: -pair[1]["value_ped"]):
            info = self.loot_markup_info_for_item(item_name)
            mu_text = self.loot_markup_display(item_name)
            item_tree.insert("", "end", values=(
                item_name, row["quantity"], f'{row["value_ped"]:.4f}', mu_text,
                f'{row["after_mu"]:.4f}', f'{percent(row["value_ped"], result["tt_loot"]):.2f}%'
            ))

        session_columns = (
            "started", "maturity", "weapon", "amplifier", "attachments",
            "ped", "tt", "after_mu", "tt_return", "mu_return", "events",
        )
        session_tree = ttk.Treeview(sessions_tab, columns=session_columns, show="headings")
        for column, title, width in [
            ("started", "Started", 165),
            ("maturity", "Maturity", 120),
            ("weapon", "Weapon", 220),
            ("amplifier", "Amplifier", 170),
            ("attachments", "Attachments", 260),
            ("ped", "PED cycled", 105),
            ("tt", "TT loot", 95),
            ("after_mu", "After MU", 95),
            ("tt_return", "TT return", 90),
            ("mu_return", "After MU return", 115),
            ("events", "Loot events", 90),
        ]:
            session_tree.heading(column, text=title)
            session_tree.column(
                column,
                width=width,
                anchor="w" if column in ("weapon", "amplifier", "attachments") else "center",
            )
        session_xscroll = ttk.Scrollbar(sessions_tab, orient="horizontal", command=session_tree.xview)
        session_tree.configure(xscrollcommand=session_xscroll.set)
        session_tree.pack(fill="both", expand=True)
        session_xscroll.pack(fill="x")
        for row in result["session_rows"]:
            session_tree.insert("", "end", values=(
                row["started_at"],
                row["maturity"],
                row.get("weapon", "") or "-",
                row.get("amplifier", "") or "-",
                ", ".join(row.get("attachments", []) or []) or "-",
                f'{row["ped_cycled"]:.4f}',
                f'{row["tt_loot"]:.4f}',
                f'{row["after_mu"]:.4f}',
                f'{percent(row["tt_loot"], row["ped_cycled"]):.2f}%',
                f'{percent(row["after_mu"], row["ped_cycled"]):.2f}%',
                row["loot_events"],
            ))

    def create_session_details_tab(self):
        top = ttk.Frame(self.session_details_tab, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="Select a session in Previous Sessions to inspect exact skill gains, averages, hunting totals, and saved parsed events.",
        ).pack(anchor="w")

        projection = ttk.LabelFrame(self.session_details_tab, text="Profession projection", padding=10)
        projection.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(projection, text="Profession:").grid(row=0, column=0, sticky="w")
        profession_combo = ttk.Combobox(
            projection,
            textvariable=self.session_projection_profession_var,
            values=sorted(PROFESSIONS.keys(), key=str.lower),
            state="readonly",
            width=45,
        )
        profession_combo.grid(row=0, column=1, sticky="w", padx=8)
        profession_combo.bind("<<ComboboxSelected>>", self.refresh_selected_session_details)
        ttk.Label(projection, text="PED cycle:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ped_entry = ttk.Entry(projection, textvariable=self.session_projection_ped_var, width=14)
        ped_entry.grid(row=0, column=3, sticky="w", padx=8)
        ped_entry.bind("<Return>", self.refresh_selected_session_details)
        ped_entry.bind("<FocusOut>", self.refresh_selected_session_details)
        ttk.Button(projection, text="Calculate", command=self.refresh_selected_session_details).grid(row=0, column=4, sticky="w", padx=4)
        projection.columnconfigure(1, weight=1)

        self.session_detail_summary_var = tk.StringVar(value="No session selected")
        summary = ttk.LabelFrame(self.session_details_tab, text="Selected session summary", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.session_detail_summary_var, justify="left").pack(anchor="w")

        skill_frame = ttk.LabelFrame(self.session_details_tab, text="Skill gains in selected session", padding=6)
        skill_frame.pack(fill="both", expand=True, padx=10, pady=6)
        columns = ("skill", "points", "tt", "tt_percent", "count", "message_percent", "avg_points", "avg_tt")
        self.session_detail_skill_tree = ttk.Treeview(skill_frame, columns=columns, show="headings", height=12)
        setup = [
            ("skill", "Skill", 260),
            ("points", "Point gain", 130),
            ("tt", "TT-equivalent", 130),
            ("tt_percent", "TT % of skills", 120),
            ("count", "Messages", 90),
            ("message_percent", "Msg % of skills", 120),
            ("avg_points", "Avg point/msg", 130),
            ("avg_tt", "Avg TT/msg", 130),
        ]
        for col, title, width in setup:
            self.session_detail_skill_tree.heading(col, text=title)
            self.session_detail_skill_tree.column(col, width=width, anchor="center" if col != "skill" else "w")
        self.make_tree_sortable(self.session_detail_skill_tree, {col: title for col, title, _ in setup})
        self.session_detail_skill_tree.pack(fill="both", expand=True)
        self.session_detail_skill_tree.bind(
            "<Double-1>",
            lambda event: self.open_skill_gain_details_from_tree(event, self.session_detail_skill_tree, "saved"),
        )

        events_frame = ttk.LabelFrame(self.session_details_tab, text="Saved parsed events (display limited, file saves all)", padding=6)
        events_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.session_detail_events_text = tk.Text(events_frame, height=10, wrap="none")
        self.session_detail_events_text.pack(fill="both", expand=True)

    def selected_session_indices_from_table(self):
        indices = []
        for iid in self.sessions_tree.selection():
            try:
                index = int(str(iid).replace("session_", ""))
            except ValueError:
                continue
            if 0 <= index < len(self.sessions):
                indices.append(index)
        return sorted(set(indices))

    def selected_session_index_from_table(self):
        indices = self.selected_session_indices_from_table()
        return indices[0] if indices else None

    def selected_session_from_table(self):
        index = self.selected_session_index_from_table()
        if index is None:
            return None
        return self.sessions[index]

    def on_session_selected(self, event=None):
        self.show_session_details(self.selected_session_from_table())
        self.refresh_loot_tab()

    def refresh_selected_session_details(self, event=None):
        self.show_session_details(self.selected_session_from_table())
        self.update_session_summary()

    def show_session_details(self, session):
        self.session_detail_skill_tree.delete(*self.session_detail_skill_tree.get_children())
        self.session_detail_events_text.delete("1.0", "end")
        if not session:
            self.session_detail_summary_var.set("No session selected")
            return

        skill_points = session.get("skill_gains_points", {}) or {}
        skill_tt = session.get("skill_gains_tt", {}) or {}
        counts = session.get("skill_gain_events_by_skill", {}) or {}
        skill_points_total = float(session.get("skill_gain_points_total", sum(float(v) for v in skill_points.values())))
        skill_tt_total = float(session.get("skill_gain_tt_total", sum(float(v) for v in skill_tt.values())))
        skill_events_total = int(sum(int(v) for v in counts.values())) if counts else 0
        avg_points = skill_points_total / skill_events_total if skill_events_total else 0.0
        avg_tt = skill_tt_total / skill_events_total if skill_events_total else 0.0
        mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip() or "-"
        ped_cycled = float(session.get('ped_cycled', 0.0))
        loot_ped = float(session.get('loot_ped_total', 0.0))
        loot_percent = percent(loot_ped, ped_cycled)
        loot_event_count = len(self.loot_events_for_session(session))
        cost_per_kill = ped_cycled / loot_event_count if loot_event_count else 0.0
        skill_tt_percent = percent(skill_tt_total, ped_cycled)
        skill_messages_per_attack = percent(skill_events_total, session.get('attacks_total', 0))
        attachments = ", ".join(session.get("attachments", []) or []) or "-"
        profession_projection_text = self.profession_projection_text(session)
        saved_skill_snapshot_count = len(self.session_skill_snapshot(session))

        defended_attacks = int(session.get("defended_attacks", session.get("jammed_attacks", 0)) or 0)
        missed_attacks = int(session.get("missed_attacks", 0) or 0)

        self.session_detail_summary_var.set(
            f"Started: {session.get('started_at', '')} | Ended: {session.get('ended_at', '')}\n"
            f"Log: {session.get('chat_log_path', '')}\n"
            f"Saved current-skills snapshot: {saved_skill_snapshot_count} skills\n"
            f"Weapon: {session.get('weapon', '-') or '-'} | Amp: {session.get('amplifier', '') or '-'} | Attachments: {attachments}\n"
            f"Mob: {mob} | Count hunting: {bool(session.get('count_hunting', False))}\n"
            f"Attacks: {session.get('attacks_total', 0)} "
            f"(hits {session.get('normal_hits', 0)}, crits {session.get('critical_hits', 0)}, "
            f"defended {defended_attacks}, misses {missed_attacks}) | "
            f"Damage: {float(session.get('damage_total', 0.0)):.1f} | PED cycled: {ped_cycled:.4f} | "
            f"Loot: {loot_ped:.4f} PED ({loot_percent:.2f}%) | Loot events/kills: {loot_event_count} | "
            f"Cost/kill: {cost_per_kill:.6f} PED\n"
            f"Skill gain messages: {skill_events_total} | Point total: {skill_points_total:.4f} | "
            f"TT-equivalent total: {skill_tt_total:.4f} ({skill_tt_percent:.2f}% of cycled) | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}% | "
            f"Avg/message: {avg_points:.6f} points / {avg_tt:.6f} TT\n"
            f"{profession_projection_text}"
        )

        all_skills = sorted(set(skill_points) | set(skill_tt) | set(counts), key=str.lower)
        for skill in all_skills:
            points = float(skill_points.get(skill, 0.0))
            tt = float(skill_tt.get(skill, 0.0))
            count = int(counts.get(skill, 0))
            self.session_detail_skill_tree.insert(
                "",
                "end",
                values=(
                    skill,
                    f"{points:.4f}",
                    f"{tt:.6f}",
                    f"{percent(tt, skill_tt_total):.2f}%",
                    count,
                    f"{percent(count, skill_events_total):.2f}%",
                    f"{(points / count) if count else 0.0:.6f}",
                    f"{(tt / count) if count else 0.0:.6f}",
                ),
            )
        self.apply_tree_sort(self.session_detail_skill_tree)

        for event in session.get("events", []) or []:
            etype = event.get("type", "")
            timestamp = event.get("timestamp", "")
            if etype == "skill_gain":
                line = f"{timestamp} | skill | {event.get('skill', '')}: +{float(event.get('delta_points', event.get('delta_tt', 0.0))):.6f} points | {event.get('message', '')}"
            elif etype in ("normal_hit", "crit"):
                line = f"{timestamp} | {etype} | damage {float(event.get('damage', 0.0)):.1f} | {event.get('message', '')}"
            elif etype in ("defended_attack", "jammed"):
                defense = event.get("defense", "jammed" if etype == "jammed" else "defended")
                line = f"{timestamp} | defended ({defense}) | {event.get('message', '')}"
            elif etype == "miss":
                line = f"{timestamp} | miss | {event.get('message', '')}"
            elif etype == "loot":
                line = f"{timestamp} | loot | +{float(event.get('value_ped', 0.0)):.4f} PED | {event.get('message', '')}"
            else:
                line = str(event)
            self.session_detail_events_text.insert("end", line + "\n")
        self.session_detail_events_text.see("1.0")

    def skill_snapshot(self):
        return {str(k): float(v) for k, v in sorted(self.current_skills.items())}

    def session_skill_snapshot(self, session):
        if not session:
            return {}
        snapshot = session.get("current_skills_at_end") or session.get("current_skills") or {}
        result = {}
        for key, value in snapshot.items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                pass
        return result

    def load_current_skills_from_selected_session(self):
        session = self.selected_session_from_table()
        if not session:
            messagebox.showwarning("No session selected", "Select a session first.")
            return

        snapshot = self.session_skill_snapshot(session)
        if not snapshot:
            messagebox.showwarning(
                "No skills snapshot",
                "This session has no saved current-skills snapshot. Only sessions saved after this update can be restored.",
            )
            return

        started = session.get("started_at", "")
        ended = session.get("ended_at", "") or "not ended"
        if not messagebox.askyesno(
            "Load skills from session",
            f"Replace current_skills.json and the current in-memory skills with the skills saved at the end of this session?\n\n"
            f"Started: {started}\nEnded: {ended}\nSkills: {len(snapshot)}",
        ):
            return

        self.current_skills = snapshot
        save_current_skills(self.current_skills)
        self.load_profession_keep_selection()
        self.refresh_session_skill_tree()
        self.update_session_summary()
        messagebox.showinfo("Skills loaded", f"Loaded {len(snapshot)} skills from the selected session.")

    def browse_chat_log(self):
        filename = filedialog.askopenfilename(
            title="Choose Entropia chat.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if filename:
            self.chat_log_path_var.set(filename)
            self.save_state()

    def save_last_log_read_at_from_ui(self):
        value = self.last_log_read_at_var.get().strip()
        if value and parse_iso_datetime(value) is None:
            messagebox.showerror("Invalid time", "Use ISO format like 2026-06-20T18:30:00 or leave it empty.")
            return False
        self.last_log_read_at_var.set(value)
        self.save_state(last_log_read_at=value)
        return True

    def clear_last_log_read_at(self):
        self.last_log_read_at_var.set("")
        self.save_state(last_log_read_at="")

    def reload_last_log_read_at(self):
        self.last_log_read_at_var.set(str(self.state.get("last_log_read_at", "") or ""))

    def load_profession(self):
        self.entries.clear()
        self.skill_tree.delete(*self.skill_tree.get_children())
        profession = PROFESSIONS[self.profession_var.get()]
        for skill_name, weight in sorted(profession["skills"].items(), key=lambda item: item[1], reverse=True):
            current = float(self.current_skills.get(skill_name, 0.0))
            self.entries[skill_name] = {"weight": float(weight), "current": current, "delta": 0.0, "new": current, "skill_gain": 0.0, "profession_gain": 0.0}
            self.skill_tree.insert("", "end", iid=skill_name, values=(skill_name, f"{weight:g}", f"{current:.4f}", "0.0000", f"{current:.4f}", "0.0000", "0.0000"))
        self.total_gain_var.set("Total profession gain: 0.0000")
        self.selected_skill_var.set("")
        self.current_var.set("0")
        self.delta_var.set("0")

    def on_skill_selected(self, event=None):
        selected = self.skill_tree.selection()
        if not selected:
            return
        skill_name = selected[0]
        data = self.entries[skill_name]
        self.selected_skill_var.set(skill_name)
        self.current_var.set(str(data["current"]))
        self.delta_var.set(str(data["delta"]))

    def apply_selected_skill(self):
        selected = self.skill_tree.selection()
        if not selected:
            messagebox.showwarning("No skill selected", "Select a skill first.")
            return
        skill_name = selected[0]
        current = parse_float(self.current_var.get(), None)
        delta = parse_float(self.delta_var.get(), None)
        if current is None or delta is None:
            messagebox.showerror("Invalid input", "Current x1 and TT delta must be numbers.")
            return
        self.entries[skill_name].update({"current": current, "delta": delta, "new": current, "skill_gain": 0.0, "profession_gain": 0.0})
        self.current_skills[skill_name] = current
        self.update_skill_tree_row(skill_name)

    def save_selected_current_skill(self):
        selected = self.skill_tree.selection()
        if not selected:
            messagebox.showwarning("No skill selected", "Select a skill first.")
            return
        skill_name = selected[0]
        current = parse_float(self.current_var.get(), None)
        if current is None:
            messagebox.showerror("Invalid input", "Current x1 must be a number.")
            return
        self.current_skills[skill_name] = current
        if skill_name in self.entries:
            self.entries[skill_name]["current"] = current
            self.update_skill_tree_row(skill_name)
        save_current_skills(self.current_skills)
        messagebox.showinfo("Saved", f"Saved {skill_name} = {current:.4f}")

    def save_current_skills_from_table(self):
        for skill_name, data in self.entries.items():
            self.current_skills[skill_name] = float(data["current"])
        save_current_skills(self.current_skills)
        messagebox.showinfo("Saved", f"Current skills saved to {CURRENT_SKILLS_FILE}")

    def reload_saved_skills(self):
        self.current_skills = load_current_skills()
        self.load_profession()
        self.load_analysis_looters_from_current_skills()
        self.refresh_session_skill_tree()
        messagebox.showinfo("Reloaded", f"Loaded skills from {CURRENT_SKILLS_FILE}")

    def calculate_profession_gain(self):
        total_profession_gain = 0.0
        for skill_name, data in self.entries.items():
            try:
                new_x = find_skill_after_tt_delta(data["current"], data["delta"])
            except Exception as ex:
                messagebox.showerror("Calculation error", f"{skill_name}: {ex}")
                return
            skill_gain = new_x - data["current"]
            profession_gain = profession_weighted_value(skill_name, skill_gain) * data["weight"] / 100.0
            total_profession_gain += profession_gain
            data.update({"new": new_x, "skill_gain": skill_gain, "profession_gain": profession_gain})
            if self.auto_update_current_skills_var.get() and data["delta"] != 0:
                data["current"] = new_x
                data["delta"] = 0.0
                self.current_skills[skill_name] = new_x
            self.update_skill_tree_row(skill_name)
        if self.auto_update_current_skills_var.get():
            save_current_skills(self.current_skills)
        self.total_gain_var.set(f"Total profession gain: {total_profession_gain:.4f}")

    def update_skill_tree_row(self, skill_name):
        if skill_name not in self.entries or not self.skill_tree.exists(skill_name):
            return
        data = self.entries[skill_name]
        self.skill_tree.item(skill_name, values=(skill_name, f"{data['weight']:g}", f"{data['current']:.4f}", f"{data['delta']:.4f}", f"{data['new']:.4f}", f"{data['skill_gain']:.4f}", f"{data['profession_gain']:.4f}"))

    def filter_names(self, names, query):
        query = (query or "").strip().lower()
        if not query:
            return names
        tokens = [token for token in query.split() if token]
        filtered = []
        for name in names:
            lower_name = name.lower()
            if all(token in lower_name for token in tokens):
                filtered.append(name)
        return filtered

    def filter_weapon_values(self, query=None):
        values = self.filter_names(self.all_weapon_names, self.weapon_filter_var.get() if query is None else query)
        self.weapon_combo.configure(values=values)
        if len(values) == 1 and query is None:
            self.weapon_var.set(values[0])
            self.on_hunting_changed()

    def filter_amplifier_values(self, query=None):
        values = self.filter_names(self.all_amplifier_names, self.amplifier_filter_var.get() if query is None else query)
        self.amplifier_combo.configure(values=[""] + values)
        if len(values) == 1 and query is None:
            self.amplifier_var.set(values[0])
            self.on_hunting_changed()

    def filter_attachment_values(self, query=None):
        values = self.filter_names(self.all_attachment_names, self.attachment_filter_var.get() if query is None else query)
        combo_values = [""] + values
        for combo in self.attachment_combos:
            combo.configure(values=combo_values)
        if len(values) == 1 and query is None:
            for attachment_var in self.attachment_vars:
                if not attachment_var.get():
                    attachment_var.set(values[0])
                    self.on_hunting_changed()
                    break

    def filter_mob_values(self, query=None):
        values = self.filter_names(self.all_mob_names, self.mob_filter_var.get() if query is None else query)
        self.mob_combo.configure(values=values)
        if len(values) == 1 and query is None:
            self.mob_var.set(values[0])
            self.on_mob_changed()

    def clear_weapon_filter(self):
        self.weapon_filter_var.set("")
        self.weapon_combo.configure(values=self.all_weapon_names)

    def clear_amplifier_filter(self):
        self.amplifier_filter_var.set("")
        self.amplifier_combo.configure(values=[""] + self.all_amplifier_names)

    def clear_attachment_filter(self):
        self.attachment_filter_var.set("")
        for combo in self.attachment_combos:
            combo.configure(values=[""] + self.all_attachment_names)

    def clear_mob_filter(self):
        self.mob_filter_var.set("")
        self.mob_combo.configure(values=self.all_mob_names)

    def resolve_hunting_setup_name(self, name: str) -> str | None:
        requested = str(name or "").strip()
        if not requested:
            return None
        requested_folded = requested.casefold()
        for existing_name in self.hunting_setups:
            if str(existing_name).casefold() == requested_folded:
                return str(existing_name)
        return None

    def refresh_hunting_setup_values(self):
        if hasattr(self, "hunting_setup_combo"):
            self.hunting_setup_combo.configure(values=sorted(self.hunting_setups.keys(), key=str.lower))

    def current_hunting_setup_payload(self):
        return {
            "weapon": self.weapon_var.get(),
            "amplifier": self.selected_amplifier(),
            "attachments": self.selected_attachments(),
            "mob": self.mob_var.get(),
            "maturity": self.maturity_var.get(),
            "count_hunting": bool(self.count_hunting_var.get()),
        }

    def save_named_hunting_setup(self):
        typed_name = self.hunting_setup_name_var.get().strip()
        if not typed_name:
            messagebox.showwarning("Setup name required", "Enter a name for this hunting setup first.")
            return

        existing_name = self.resolve_hunting_setup_name(typed_name)
        save_name = existing_name or typed_name
        if existing_name and not messagebox.askyesno(
            "Update hunting setup",
            f"Replace the saved setup '{existing_name}' with the current weapon, attachments, mob, and maturity?",
        ):
            return

        self.hunting_setups[save_name] = self.current_hunting_setup_payload()
        save_json(HUNTING_SETUPS_FILE, self.hunting_setups)
        self.hunting_setup_name_var.set(save_name)
        self.refresh_hunting_setup_values()
        self.hunting_setup_status_var.set(f"Saved setup: {save_name}")
        self.save_state()

    def load_named_hunting_setup(self, event=None):
        requested_name = self.hunting_setup_name_var.get().strip()
        setup_name = self.resolve_hunting_setup_name(requested_name)
        if not setup_name:
            if event is None:
                messagebox.showwarning("Setup not found", "Choose a saved hunting setup first.")
            return

        setup = self.hunting_setups.get(setup_name) or {}
        if not isinstance(setup, dict):
            messagebox.showerror("Invalid setup", f"The saved setup '{setup_name}' is not valid.")
            return

        weapon = str(setup.get("weapon", "") or "")
        amplifier = str(setup.get("amplifier", "") or "")
        attachments = list(setup.get("attachments", []) or [])[:3]
        mob = str(setup.get("mob", "") or "")
        maturity = str(setup.get("maturity", "") or "")

        self.clear_weapon_filter()
        self.clear_amplifier_filter()
        self.clear_attachment_filter()
        self.clear_mob_filter()

        self.weapon_var.set(weapon if weapon in WEAPONS else "")
        self.amplifier_var.set(amplifier if amplifier in AMPLIFIERS else "")
        while len(attachments) < 3:
            attachments.append("")
        for attachment_var, attachment_name in zip(self.attachment_vars, attachments):
            attachment_var.set(attachment_name if attachment_name in ATTACHMENTS else "")

        self.mob_var.set(mob if mob in MOBS else "")
        self.maturity_var.set("")
        self.update_maturity_values()
        valid_maturities = set(((MOBS.get(self.mob_var.get()) or {}).get("maturities") or {}).keys())
        if maturity in valid_maturities:
            self.maturity_var.set(maturity)

        self.count_hunting_var.set(bool(setup.get("count_hunting", False)))
        self.hunting_setup_name_var.set(setup_name)
        self.refresh_hunting_info()
        self.hunting_setup_status_var.set(f"Loaded setup: {setup_name}")
        self.save_state()

    def delete_named_hunting_setup(self):
        requested_name = self.hunting_setup_name_var.get().strip()
        setup_name = self.resolve_hunting_setup_name(requested_name)
        if not setup_name:
            messagebox.showwarning("Setup not found", "Choose a saved hunting setup first.")
            return
        if not messagebox.askyesno("Delete hunting setup", f"Delete the saved setup '{setup_name}'?"):
            return

        del self.hunting_setups[setup_name]
        save_json(HUNTING_SETUPS_FILE, self.hunting_setups)
        self.hunting_setup_name_var.set("")
        self.hunting_setup_status_var.set(f"Deleted setup: {setup_name}")
        self.refresh_hunting_setup_values()
        self.save_state()

    def selected_attachments(self):
        attachments = []
        for attachment_var in self.attachment_vars[:3]:
            name = attachment_var.get()
            if name in ATTACHMENTS:
                attachments.append(name)
            elif name:
                attachment_var.set("")
        return attachments

    def selected_amplifier(self):
        amplifier = self.amplifier_var.get()
        if amplifier in AMPLIFIERS:
            return amplifier
        if amplifier:
            self.amplifier_var.set("")
        return ""

    def on_mob_changed(self):
        self.update_maturity_values()
        self.on_hunting_changed()

    def on_hunting_changed(self):
        self.refresh_hunting_info()
        self.save_state()

    def update_maturity_values(self):
        mob = MOBS.get(self.mob_var.get()) or {}
        maturities = sorted((mob.get("maturities") or {}).keys())
        self.maturity_combo.configure(values=maturities)
        if maturities and self.maturity_var.get() not in maturities:
            self.maturity_var.set(maturities[0])

    def refresh_hunting_info(self):
        weapon_name = self.weapon_var.get()
        amplifier_name = self.selected_amplifier()
        attachments = self.selected_attachments()
        cost = hunting_setup_cost_per_shot_ped(weapon_name, amplifier_name, attachments)
        self.weapon_cost_var.set(f"Cost/shot: {cost:.6f} PED")

        mob = MOBS.get(self.mob_var.get()) or {}
        maturity = (mob.get("maturities") or {}).get(self.maturity_var.get()) or {}
        planets = ", ".join(mob.get("planets") or []) or "-"
        hp = maturity.get("hp", "-")
        level = maturity.get("level", "-")
        self.mob_info_var.set(f"Planet: {planets} | HP: {hp} | Level: {level}")

    def start_sync(self):
        if self.monitoring:
            self.stop_sync()

        path = Path(self.chat_log_path_var.get()).expanduser()
        if not path.exists():
            messagebox.showerror("chat.log not found", "Choose a valid chat.log file first.")
            return

        mode = self.sync_start_mode_var.get()
        previous_state = dict(self.state)
        last_log_read_at_to_save = None

        if mode == "From start of log":
            start_offset = 0
            self.log_time_cutoff_at = None
            last_log_read_at_to_save = ""
            resume_message = "Started sync session from beginning of log"
        elif mode == "From chosen time":
            chosen_time = self.last_log_read_at_var.get().strip()
            cutoff_at = parse_iso_datetime(chosen_time) if chosen_time else None
            if chosen_time and cutoff_at is None:
                messagebox.showerror("Invalid time", "Use ISO format like 2026-06-20T18:30:00 or leave it empty.")
                return
            start_offset = 0
            self.log_time_cutoff_at = cutoff_at
            last_log_read_at_to_save = chosen_time
            if chosen_time:
                resume_message = f"Started sync session from beginning of log; skipping lines before {chosen_time}"
            else:
                resume_message = "Started sync session from beginning of log with no time cutoff"
        elif mode == "From end of log":
            try:
                start_offset = int(path.stat().st_size)
            except OSError as ex:
                messagebox.showerror("chat.log read error", str(ex))
                return
            self.log_time_cutoff_at = None
            latest_log_time = newest_log_timestamp_at_or_before(path, start_offset)
            last_log_read_at_to_save = latest_log_time.isoformat(timespec="seconds") if latest_log_time else ""
            resume_message = f"Started sync session from end of log at offset {start_offset}"
        else:
            # Resume only when the saved offset belongs to the same unchanged log.
            # Size checks alone are not enough: chat.log can be cleared/replaced and
            # later grow past the old offset, which would make us skip valid data.
            saved_path = previous_state.get("chat_log_path", "")
            saved_offset = int(previous_state.get("last_log_offset", 0) or 0)
            saved_fingerprint = previous_state.get("last_log_fingerprint", "")
            saved_last_read_at = previous_state.get("last_log_read_at", "")

            if TRACKER_STATE_FILE.exists() and can_resume_log(path, saved_path, saved_offset, saved_fingerprint):
                start_offset = saved_offset
                self.log_time_cutoff_at = None
                resume_message = f"Started sync session from saved offset {start_offset}"
            else:
                start_offset = 0
                # If the log was cleared/replaced, we must scan from byte 0, but
                # should not re-apply older lines. Only lines whose chat timestamp
                # is at/after last_log_read_at are processed.
                self.log_time_cutoff_at = parse_iso_datetime(saved_last_read_at) if TRACKER_STATE_FILE.exists() else None
                if self.log_time_cutoff_at is None:
                    resume_message = "Started sync session from beginning of log"
                else:
                    resume_message = f"Started sync session from beginning of log; skipping lines before {saved_last_read_at}"

        self.log_offset = start_offset
        if last_log_read_at_to_save is None:
            self.save_state()
        else:
            self.save_state(last_log_read_at=last_log_read_at_to_save)

        # Drop any stale worker messages from a previous stopped session.
        while True:
            try:
                self.reader_queue.get_nowait()
            except queue.Empty:
                break
        self.reader_done_pending = False
        self.reader_active = False
        self.reader_stop_event.clear()

        self.current_session = MonitorSession(
            id=datetime.now().strftime("%Y%m%d_%H%M%S"),
            started_at=now_iso(),
            chat_log_path=str(path),
            start_offset=start_offset,
            end_offset=start_offset,
            log_cutoff_at=self.log_time_cutoff_at.isoformat(timespec="seconds") if self.log_time_cutoff_at else "",
            weapon=self.weapon_var.get(),
            amplifier=self.selected_amplifier(),
            attachments=self.selected_attachments(),
            mob=self.mob_var.get(),
            maturity=self.maturity_var.get(),
            count_hunting=self.count_hunting_var.get(),
            current_skills_at_start=self.skill_snapshot(),
            current_skills_at_end=self.skill_snapshot(),
        )
        self.monitoring = True
        self.sync_paused = False
        if hasattr(self, "pause_sync_button"):
            self.pause_sync_button.configure(text="Pause Sync")
        self.monitor_status_var.set("Syncing")
        self.append_event(resume_message)
        self.live_ui_dirty = True
        self.refresh_live_ui(force=True)
        self.start_log_reader_until_current_eof()

    def stop_sync(self):
        if not self.monitoring or self.current_session is None:
            self.monitor_status_var.set("Stopped")
            return

        # Ask the background reader to stop, then apply anything it already
        # parsed before saving the session. Waiting prevents stale worker
        # batches from a stopped session from being applied to a restarted one.
        self.reader_stop_event.set()
        if self.reader_thread is not None and self.reader_thread.is_alive():
            self.reader_thread.join()
        self.process_reader_queue(max_batches=1_000_000, time_budget_ms=None, force_refresh=True)

        self.current_session.ended_at = now_iso()
        self.current_session.end_offset = self.log_offset
        self.current_session.current_skills_at_end = self.skill_snapshot()
        self.current_session.total_profession_gain_by_profession = self.calculate_profession_gains_for_session(self.current_session)
        self.maybe_persist_live_state(force=True)
        self.refresh_live_ui(force=True)
        self.sessions.append(asdict(self.current_session))
        save_json(SESSIONS_FILE, self.sessions)
        self.append_event("Stopped sync session, saved current skills, and saved the session")
        self.flush_event_text()
        self.current_session = None
        self.log_time_cutoff_at = None
        self.monitoring = False
        self.sync_paused = False
        if hasattr(self, "pause_sync_button"):
            self.pause_sync_button.configure(text="Pause Sync")
        self.reader_active = False
        self.reader_done_pending = False
        self.monitor_status_var.set("Stopped")
        self.monitor_progress_var.set("")
        self.session_summary_var.set("No active session")
        self.refresh_sessions_table()
        self.save_state()

    def toggle_pause_sync(self):
        if self.sync_paused:
            self.resume_sync()
        else:
            self.pause_sync()

    def pause_sync(self):
        """Pause log reading but keep the current session open.

        Stop Sync still ends and saves the session. Pause Sync only freezes the
        reader at the current byte offset, so Resume Sync can continue into the
        same MonitorSession without creating a new run.
        """
        if not self.monitoring or self.current_session is None:
            self.monitor_status_var.set("Stopped")
            return
        if self.sync_paused:
            return

        self.sync_paused = True
        self.reader_stop_event.set()
        if self.reader_thread is not None and self.reader_thread.is_alive():
            self.reader_thread.join()
        self.process_reader_queue(max_batches=1_000_000, time_budget_ms=None, force_refresh=True)
        self.reader_active = False
        self.reader_done_pending = False
        self.maybe_persist_live_state(force=True)
        self.refresh_live_ui(force=True)
        if hasattr(self, "pause_sync_button"):
            self.pause_sync_button.configure(text="Resume Sync")
        self.monitor_status_var.set("Paused")
        self.monitor_progress_var.set(f"Paused at offset {self.log_offset:,}. Press Resume Sync to continue this same session.")
        self.append_event(f"Paused sync at offset {self.log_offset}")

    def resume_sync(self):
        if not self.monitoring or self.current_session is None:
            self.monitor_status_var.set("Stopped")
            self.sync_paused = False
            if hasattr(self, "pause_sync_button"):
                self.pause_sync_button.configure(text="Pause Sync")
            return
        if not self.sync_paused:
            return

        self.sync_paused = False
        self.reader_stop_event.clear()
        if hasattr(self, "pause_sync_button"):
            self.pause_sync_button.configure(text="Pause Sync")
        self.monitor_status_var.set("Syncing")
        self.monitor_progress_var.set(f"Resuming from offset {self.log_offset:,}...")
        self.append_event(f"Resumed sync from offset {self.log_offset}")
        self.start_log_reader_until_current_eof()

    def monitor_tick(self):
        if self.monitoring:
            self.process_reader_queue(max_batches=8, time_budget_ms=45)
            self.refresh_live_ui()
            self.maybe_persist_live_state()
            if self.sync_paused:
                if not self.reader_active:
                    self.monitor_status_var.set("Paused")
            elif not self.reader_active and not self.reader_done_pending:
                self.start_log_reader_until_current_eof()
        self.root.after(100, self.monitor_tick)

    def start_log_reader_until_current_eof(self):
        """Start a background reader for everything currently present in chat.log.

        The worker reads/parses the file off the Tkinter thread. The UI thread
        receives parsed batches through a queue and applies them gradually.
        """
        if self.reader_active:
            return

        path = Path(self.chat_log_path_var.get()).expanduser()
        if not path.exists():
            self.monitor_status_var.set("chat.log missing")
            return

        try:
            current_size = path.stat().st_size
        except OSError as ex:
            self.monitor_status_var.set(f"Read error: {ex}")
            return

        # Detect clear/replace before launching the worker. When reset happens,
        # scan from byte 0 but keep the last real chat timestamp cutoff.
        if current_size < self.log_offset:
            cutoff_text = self.state.get("last_log_read_at", "")
            self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
            self.append_event(f"chat.log became smaller than saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
            self.log_offset = 0
        elif self.log_offset > 0:
            saved_fingerprint = self.state.get("last_log_fingerprint", "")
            current_fingerprint = log_resume_fingerprint(path, self.log_offset)
            if saved_fingerprint and current_fingerprint and saved_fingerprint != current_fingerprint:
                cutoff_text = self.state.get("last_log_read_at", "")
                self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
                self.append_event(f"chat.log content changed before saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
                self.log_offset = 0

        if current_size <= self.log_offset:
            self.save_state()
            self.monitor_status_var.set("Syncing")
            self.monitor_progress_var.set("Waiting for new log lines...")
            return

        start_offset = int(self.log_offset)
        end_offset = int(current_size)
        cutoff_at = self.log_time_cutoff_at

        self.reader_stop_event.clear()
        self.reader_active = True
        self.reader_done_pending = False
        self.reader_final_offset = None
        self.reader_final_last_read_at = ""
        self.reader_error = ""
        self.reader_processed_batches = 0
        self.monitor_status_var.set("Reading log...")
        self.monitor_progress_var.set(f"Reading bytes {start_offset:,} -> {end_offset:,}...")

        def worker():
            batch = []
            skipped_by_time = 0
            newest_line_at = None
            lines_seen = 0
            last_progress = time.time()
            last_offset = start_offset
            try:
                with path.open("rb") as handle:
                    handle.seek(start_offset)
                    while handle.tell() < end_offset and not self.reader_stop_event.is_set():
                        raw = handle.readline()
                        if not raw:
                            break
                        last_offset = handle.tell()
                        lines_seen += 1
                        line = raw.decode("utf-8", errors="replace")
                        line_at = ChatLogParser.parse_line_timestamp(line)
                        if line_at is not None and (newest_line_at is None or line_at > newest_line_at):
                            newest_line_at = line_at
                        event = ChatLogParser.parse_line(line)
                        if event:
                            if not event_is_after_cutoff(event, cutoff_at):
                                skipped_by_time += 1
                            else:
                                batch.append(event)

                        if len(batch) >= 200:
                            self.reader_queue.put(("batch", batch, skipped_by_time, newest_line_at.isoformat(timespec="seconds") if newest_line_at else "", last_offset))
                            batch = []
                            skipped_by_time = 0

                        now = time.time()
                        if now - last_progress >= 0.35:
                            self.reader_queue.put(("progress", lines_seen, last_offset, end_offset))
                            last_progress = now

                if batch or skipped_by_time:
                    self.reader_queue.put(("batch", batch, skipped_by_time, newest_line_at.isoformat(timespec="seconds") if newest_line_at else "", last_offset))
                self.reader_queue.put(("done", last_offset, newest_line_at.isoformat(timespec="seconds") if newest_line_at else ""))
            except Exception as ex:
                self.reader_queue.put(("error", str(ex)))

        self.reader_thread = threading.Thread(target=worker, name="chat-log-reader", daemon=True)
        self.reader_thread.start()

    def process_reader_queue(self, max_batches=3, time_budget_ms=45, force_refresh=False):
        """Apply parsed batches without monopolizing Tkinter's event loop.

        The worker may parse a large historical log much faster than Tkinter can
        redraw widgets. Data application therefore has a small time budget per
        tick, while visual refreshes and persistence are handled separately.
        """
        processed_batches = 0
        refresh_needed = False
        started = time.perf_counter()

        while processed_batches < max_batches:
            if time_budget_ms is not None and (time.perf_counter() - started) * 1000.0 >= float(time_budget_ms):
                break
            try:
                item = self.reader_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "progress":
                _, lines_seen, current_offset, end_offset = item
                progress_percent = (current_offset / end_offset * 100.0) if end_offset else 100.0
                self.monitor_progress_var.set(
                    f"Scanning log: {progress_percent:.1f}% | lines checked: {lines_seen:,} | "
                    f"offset {current_offset:,}/{end_offset:,} | queued batches: {self.reader_queue.qsize():,}"
                )
                continue

            if kind == "batch":
                _, events, skipped_by_time, newest_iso, batch_end_offset = item
                if skipped_by_time:
                    self.append_event(f"Skipped {skipped_by_time} old parsed events before last_log_read_at")
                for event in events:
                    self.apply_event(event)
                self.log_offset = int(batch_end_offset)
                if self.current_session is not None:
                    self.current_session.end_offset = self.log_offset
                if newest_iso:
                    self.pending_last_log_read_at = str(newest_iso)
                processed_batches += 1
                self.reader_processed_batches += 1
                refresh_needed = True
                self.live_ui_dirty = True
                self.monitor_progress_var.set(
                    f"Applied {len(events):,} parsed events | offset {self.log_offset:,} | "
                    f"queued batches: {self.reader_queue.qsize():,}"
                )
                continue

            if kind == "done":
                final_offset = item[1]
                final_last_read_at = item[2] if len(item) > 2 else ""
                self.reader_active = False
                self.reader_done_pending = True
                self.reader_final_offset = int(final_offset)
                self.reader_final_last_read_at = str(final_last_read_at or "")
                continue

            if kind == "error":
                _, message = item
                self.reader_active = False
                self.reader_done_pending = False
                self.monitor_status_var.set(f"Read error: {message}")
                self.append_event(f"Read error: {message}")
                self.live_ui_dirty = True
                continue

        if refresh_needed or force_refresh:
            self.refresh_live_ui(force=force_refresh)
            self.maybe_persist_live_state(force=force_refresh)

        if self.reader_done_pending and not self.reader_active:
            try:
                has_pending_items = self.reader_queue.qsize() > 0
            except NotImplementedError:
                has_pending_items = False
            if not has_pending_items:
                if self.reader_final_offset is not None:
                    self.log_offset = int(self.reader_final_offset)
                if self.current_session is not None:
                    self.current_session.end_offset = self.log_offset
                if self.reader_final_last_read_at:
                    self.pending_last_log_read_at = self.reader_final_last_read_at
                self.reader_done_pending = False
                self.reader_final_last_read_at = ""
                self.maybe_persist_live_state(force=True)
                self.refresh_live_ui(force=True)
                self.monitor_status_var.set("Syncing")
                self.monitor_progress_var.set(f"Caught up. Current offset: {self.log_offset:,}. Waiting for new lines...")
                if self.log_time_cutoff_at is not None:
                    self.log_time_cutoff_at = None
                    if self.current_session is not None:
                        self.current_session.log_cutoff_at = ""

    def read_new_log_lines(self):
        path = Path(self.chat_log_path_var.get()).expanduser()
        if not path.exists():
            self.monitor_status_var.set("chat.log missing")
            return

        current_size = path.stat().st_size
        if current_size < self.log_offset:
            cutoff_text = self.state.get("last_log_read_at", "")
            self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
            self.append_event(f"chat.log became smaller than saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
            self.log_offset = 0
        elif self.log_offset > 0:
            saved_fingerprint = self.state.get("last_log_fingerprint", "")
            current_fingerprint = log_resume_fingerprint(path, self.log_offset)
            if saved_fingerprint and current_fingerprint and saved_fingerprint != current_fingerprint:
                cutoff_text = self.state.get("last_log_read_at", "")
                self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
                self.append_event(f"chat.log content changed before saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
                self.log_offset = 0

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.log_offset)
                lines = handle.readlines()
                self.log_offset = handle.tell()
        except OSError as ex:
            self.monitor_status_var.set(f"Read error: {ex}")
            return

        if not lines:
            self.save_state()
            return

        skipped_by_time = 0
        processed_after_cutoff = 0
        newest_line_at = None
        for line in lines:
            line_at = ChatLogParser.parse_line_timestamp(line)
            if line_at is not None and (newest_line_at is None or line_at > newest_line_at):
                newest_line_at = line_at
            event = ChatLogParser.parse_line(line)
            if event:
                if not event_is_after_cutoff(event, self.log_time_cutoff_at):
                    skipped_by_time += 1
                    continue
                processed_after_cutoff += 1
                self.apply_event(event)

        if skipped_by_time:
            self.append_event(f"Skipped {skipped_by_time} old parsed events before last_log_read_at")
        if self.log_time_cutoff_at is not None and processed_after_cutoff:
            self.log_time_cutoff_at = None
            if self.current_session is not None:
                self.current_session.log_cutoff_at = ""

        last_read_at = newest_line_at.isoformat(timespec="seconds") if newest_line_at else ""
        if last_read_at:
            self.pending_last_log_read_at = last_read_at
        self.live_ui_dirty = True
        self.maybe_persist_live_state(force=True)
        self.refresh_live_ui(force=True)

    def apply_event(self, event):
        if self.current_session is None:
            return
        session = self.current_session
        previous_event_type = session.events[-1].get("type", "") if session.events else ""
        # Keep all parsed events in the session file. Older versions kept only
        # the last 300 events; that made old-session fallback reconstruction lose
        # earlier loot events. UI text widgets still limit what they display.
        session.events.append(event)

        event_type = event["type"]
        if event_type == "skill_gain":
            skill = event["skill"]
            # Chat.log gain values are skill-point increments. Do NOT pass them through
            # the TT conversion as if they were TT deltas; that caused huge point gains.
            point_gain = float(event["delta_tt"])
            event["delta_points"] = point_gain
            old_points = float(self.current_skills.get(skill, 0.0))
            new_points = old_points + point_gain
            try:
                tt_gain = skill_tt_value(new_points) - skill_tt_value(old_points)
            except Exception:
                tt_gain = 0.0
            event["skill_points_before"] = old_points
            event["skill_points_after"] = new_points
            event["tt_gain"] = tt_gain
            self.current_skills[skill] = new_points
            session.skill_gains_points[skill] = session.skill_gains_points.get(skill, 0.0) + point_gain
            session.skill_gains_tt[skill] = session.skill_gains_tt.get(skill, 0.0) + tt_gain
            session.skill_gain_events_by_skill[skill] = session.skill_gain_events_by_skill.get(skill, 0) + 1
            session.skill_gain_points_total += point_gain
            session.skill_gain_tt_total += tt_gain
            self.append_event(f"{event['timestamp']} skill +{point_gain:.4f} pts: {skill} ({old_points:.4f} -> {new_points:.4f})")
        elif event_type in ("normal_hit", "crit", "defended_attack", "miss", "jammed"):
            session.attacks_total += 1
            if event_type == "normal_hit":
                session.normal_hits += 1
                session.damage_total += float(event["damage"])
                self.append_event(f"{event['timestamp']} hit {event['damage']:.1f}")
            elif event_type == "crit":
                session.critical_hits += 1
                session.damage_total += float(event["damage"])
                self.append_event(f"{event['timestamp']} CRIT {event['damage']:.1f}")
            elif event_type in ("defended_attack", "jammed"):
                session.defended_attacks += 1
                defense = event.get("defense", "jammed" if event_type == "jammed" else "defended")
                self.append_event(f"{event['timestamp']} target {defense}")
            else:
                session.missed_attacks += 1
                self.append_event(f"{event['timestamp']} missed (hit ability)")
            if session.count_hunting:
                session.ped_cycled += hunting_setup_cost_per_shot_ped(
                    session.weapon,
                    session.amplifier,
                    session.attachments,
                )
        elif event_type == "loot":
            if ignored_loot_item_name(event.get("item", "")):
                return
            loot_value = float(event["value_ped"])
            self.add_loot_to_session(session, event, previous_event_type)
            session.loot_ped_total += loot_value
            self.append_event(
                f"{event['timestamp']} loot +{loot_value:.4f} PED | "
                f"{event.get('item', 'Unknown item')} x{int(event.get('quantity', 1) or 1)}"
            )

        session.end_offset = self.log_offset
        self.live_ui_dirty = True

    def add_loot_to_session(self, session: MonitorSession, event, previous_event_type: str):
        item = event.get("item") or "Unknown item"
        if ignored_loot_item_name(item):
            return
        loot_value = float(event.get("value_ped", 0.0) or 0.0)
        quantity = int(event.get("quantity", 0) or 0)
        if quantity <= 1 and item in STACKABLE_ITEM_PED_VALUE:
            quantity = parse_loot_item_quantity(item, item, loot_value)
        if quantity <= 0:
            quantity = 1
        event["quantity"] = quantity
        loot_events = session.loot_events

        continue_previous = bool(loot_events and previous_event_type == "loot")
        if not continue_previous and loot_events and previous_event_type == "skill_gain":
            previous_time = parse_chat_timestamp(str(loot_events[-1].get("ended_at", "")))
            current_time = parse_chat_timestamp(str(event.get("timestamp", "")))
            if previous_time is not None and current_time is not None:
                continue_previous = abs((current_time - previous_time).total_seconds()) <= LOOT_EVENT_CONTINUE_SECONDS

        if continue_previous:
            loot_event = loot_events[-1]
        else:
            previous_cost = float(getattr(session, "_loot_cost_accounted", 0.0) or 0.0)
            event_cost = max(0.0, float(session.ped_cycled) - previous_cost)
            setattr(session, "_loot_cost_accounted", previous_cost + event_cost)
            loot_event = {
                "index": len(loot_events) + 1,
                "started_at": event.get("timestamp", ""),
                "ended_at": event.get("timestamp", ""),
                "value_ped": 0.0,
                "cost_ped": event_cost,
                "items": {},
                "messages": [],
            }
            loot_events.append(loot_event)

        loot_event["ended_at"] = event.get("timestamp", loot_event.get("ended_at", ""))
        loot_event["value_ped"] = float(loot_event.get("value_ped", 0.0) or 0.0) + loot_value
        loot_event.setdefault("items", {})[item] = int(loot_event.setdefault("items", {}).get(item, 0) or 0) + quantity
        loot_event.setdefault("messages", []).append(event.get("message", ""))
        session.loot_event_count = len(loot_events)

    def calculate_profession_gains_for_session(self, session: MonitorSession):
        gains = {}
        for profession_name, profession in PROFESSIONS.items():
            total = 0.0
            for skill, weight in profession["skills"].items():
                weighted_gain = profession_weighted_value(
                    skill,
                    session.skill_gains_points.get(skill, 0.0),
                )
                total += weighted_gain * float(weight) / 100.0
            if total:
                gains[profession_name] = total
        return gains

    def calculate_profession_projection(self, session, profession_name: str, ped_cycle: float | None = None) -> float:
        profession = PROFESSIONS.get(profession_name)
        if not profession:
            return 0.0
        if isinstance(session, MonitorSession):
            session = asdict(session)

        skill_tt = session.get("skill_gains_tt", {}) or {}
        ped_cycled = float(session.get("ped_cycled", 0.0))
        total = 0.0

        for skill, weight in profession["skills"].items():
            if ped_cycle is None:
                tt_delta = float(skill_tt.get(skill, 0.0))
            else:
                if ped_cycled <= 0:
                    tt_delta = 0.0
                else:
                    tt_delta = float(skill_tt.get(skill, 0.0)) / ped_cycled * float(ped_cycle)
            current_points = float(self.current_skills.get(skill, 0.0))
            try:
                point_gain = find_skill_after_tt_delta(current_points, tt_delta) - current_points
            except Exception:
                point_gain = 0.0
            total += profession_weighted_value(skill, point_gain) * float(weight) / 100.0

        return total

    def selected_projection_profession(self) -> str:
        profession_name = self.session_projection_profession_var.get()
        if profession_name in PROFESSIONS:
            return profession_name
        profession_name = "Animal Looter" if "Animal Looter" in PROFESSIONS else next(iter(PROFESSIONS), "")
        self.session_projection_profession_var.set(profession_name)
        return profession_name

    def selected_projection_ped_cycle(self) -> float | None:
        ped_cycle = parse_float(self.session_projection_ped_var.get(), None)
        if ped_cycle is None or ped_cycle < 0:
            return None
        return ped_cycle

    def format_ped_cycle(self, ped_cycle: float) -> str:
        return f"{ped_cycle:,.2f}".rstrip("0").rstrip(".")

    def profession_projection_text(self, session) -> str:
        profession_name = self.selected_projection_profession()
        ped_cycle = self.selected_projection_ped_cycle()
        if ped_cycle is None:
            return f"{profession_name} projected gain: invalid PED cycle"

        projected = self.calculate_profession_projection(session, profession_name, ped_cycle)
        return f"{profession_name} projected gain at {self.format_ped_cycle(ped_cycle)} PED: {projected:.4f}"

    def refresh_session_skill_tree(self):
        self.session_skill_tree.delete(*self.session_skill_tree.get_children())
        if self.current_session is None:
            return
        total_tt_gain = float(self.current_session.skill_gain_tt_total)
        total_gain_messages = sum(int(v) for v in self.current_session.skill_gain_events_by_skill.values())
        for skill, tt_gain in sorted(self.current_session.skill_gains_tt.items(), key=lambda item: item[0].lower()):
            point_gain = self.current_session.skill_gains_points.get(skill, 0.0)
            gain_count = self.current_session.skill_gain_events_by_skill.get(skill, 0)
            current = self.current_skills.get(skill, 0.0)
            self.session_skill_tree.insert(
                "",
                "end",
                values=(
                    skill,
                    f"{tt_gain:.4f}",
                    f"{percent(tt_gain, total_tt_gain):.2f}%",
                    f"{point_gain:.4f}",
                    gain_count,
                    f"{percent(gain_count, total_gain_messages):.2f}%",
                    f"{current:.4f}",
                ),
            )
        self.apply_tree_sort(self.session_skill_tree)

    def update_session_summary(self):
        if self.current_session is None:
            self.session_summary_var.set("No active session")
            return
        s = self.current_session
        top_professions = sorted(s.total_profession_gain_by_profession.items(), key=lambda item: item[1], reverse=True)[:5]
        top_text = ", ".join(f"{name}: +{value:.4f}" for name, value in top_professions) or "-"
        total_skill_gain_events = sum(s.skill_gain_events_by_skill.values())
        loot_percent = percent(s.loot_ped_total, s.ped_cycled)
        loot_event_count = len(s.loot_events)
        cost_per_kill = s.ped_cycled / loot_event_count if loot_event_count else 0.0
        skill_tt_percent = percent(s.skill_gain_tt_total, s.ped_cycled)
        skill_messages_per_attack = percent(total_skill_gain_events, s.attacks_total)
        top_skills = []
        for skill, tt_gain in sorted(s.skill_gains_tt.items(), key=lambda item: item[1], reverse=True)[:5]:
            gain_count = int(s.skill_gain_events_by_skill.get(skill, 0))
            top_skills.append(
                f"{skill}: {tt_gain:.4f} TT ({percent(tt_gain, s.skill_gain_tt_total):.1f}% TT, "
                f"{percent(gain_count, total_skill_gain_events):.1f}% msgs)"
            )
        top_skills_text = "; ".join(top_skills) or "-"
        attachments = ", ".join(s.attachments or []) or "-"
        profession_projection_text = self.profession_projection_text(s)
        self.session_summary_var.set(
            f"Started: {s.started_at} | Weapon: {s.weapon or '-'} | Amp: {s.amplifier or '-'} | Attachments: {attachments}\n"
            f"Mob: {s.mob or '-'} {s.maturity or ''}\n"
            f"Attacks: {s.attacks_total} (hits {s.normal_hits}, crits {s.critical_hits}, "
            f"defended {s.defended_attacks}, misses {s.missed_attacks}) | "
            f"Damage: {s.damage_total:.1f} | PED cycled: {s.ped_cycled:.4f} | Loot: {s.loot_ped_total:.4f} PED ({loot_percent:.2f}%) | "
            f"Loot events/kills: {loot_event_count} | Cost/kill: {cost_per_kill:.6f} PED\n"
            f"Skill gains: {total_skill_gain_events} messages | Point total: {s.skill_gain_points_total:.4f} | "
            f"TT-equivalent total: {s.skill_gain_tt_total:.4f} ({skill_tt_percent:.2f}% of cycled) | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}%\n"
            f"Top skills: {top_skills_text}\n"
            f"Top profession gains: {top_text}\n"
            f"{profession_projection_text}"
        )

    def append_event(self, text: str):
        # Writing one Tk Text row per parsed event is extremely expensive. Keep
        # the latest messages in a bounded queue and flush them in one insert.
        self.pending_event_lines.append(str(text))
        if not self.monitoring:
            self.flush_event_text()

    def flush_event_text(self):
        if not self.pending_event_lines or not hasattr(self, "event_text"):
            return
        lines = list(self.pending_event_lines)
        self.pending_event_lines.clear()
        self.event_text.insert("end", "\n".join(lines) + "\n")
        try:
            line_count = int(self.event_text.index("end-1c").split(".")[0])
            surplus = line_count - int(self.recent_event_line_limit)
            if surplus > 0:
                self.event_text.delete("1.0", f"{surplus + 1}.0")
        except (ValueError, tk.TclError):
            pass
        self.event_text.see("end")

    def refresh_profession_current_values(self):
        # Update only existing rows. Rebuilding the complete profession table on
        # every log batch caused selection flicker and large UI stalls.
        for skill_name, data in self.entries.items():
            current = float(self.current_skills.get(skill_name, 0.0))
            if current == float(data.get("current", 0.0)):
                continue
            data["current"] = current
            if float(data.get("delta", 0.0)) == 0.0:
                data["new"] = current
                data["skill_gain"] = 0.0
                data["profession_gain"] = 0.0
            self.update_skill_tree_row(skill_name)

    def maybe_persist_live_state(self, force=False):
        if not self.monitoring or self.current_session is None:
            return
        now = time.monotonic()
        if not force and now - self.live_persist_last_at < self.live_persist_interval:
            return
        self.current_session.current_skills_at_end = self.skill_snapshot()
        save_current_skills(self.current_skills)
        if self.pending_last_log_read_at:
            self.save_state(last_log_read_at=self.pending_last_log_read_at)
            self.pending_last_log_read_at = ""
        else:
            self.save_state()
        self.live_persist_last_at = now

    def refresh_live_ui(self, force=False):
        now = time.monotonic()
        if not force:
            if not self.live_ui_dirty and not self.pending_event_lines:
                return
            if now - self.live_ui_last_refresh_at < self.live_ui_refresh_interval:
                return

        if self.current_session is not None:
            self.current_session.current_skills_at_end = self.skill_snapshot()
            self.current_session.total_profession_gain_by_profession = self.calculate_profession_gains_for_session(self.current_session)
            self.refresh_session_skill_tree()
            self.update_session_summary()
            self.refresh_profession_current_values()

            if self.is_tab_active(self.loot_tab) and (
                force or now - self.last_loot_live_refresh_at >= self.loot_live_refresh_interval
            ):
                self.refresh_loot_tab()
                self.last_loot_live_refresh_at = now

        self.flush_event_text()
        self.live_ui_dirty = False
        self.live_ui_last_refresh_at = now

    def load_profession_keep_selection(self):
        selected_profession = self.profession_var.get()
        selected_skill = self.selected_skill_var.get()
        self.load_profession()
        self.profession_var.set(selected_profession)
        if selected_skill and self.skill_tree.exists(selected_skill):
            self.skill_tree.selection_set(selected_skill)
            self.skill_tree.focus(selected_skill)
            self.on_skill_selected()

    def refresh_sessions_table(self):
        self.sessions_tree.delete(*self.sessions_tree.get_children())
        valid_analysis_ids = {
            str(session.get("id", "") or "")
            for session in self.analysis_sessions
            if isinstance(session, dict) and session.get("id")
        }
        start_index = max(0, len(self.sessions) - 200)
        for index in range(len(self.sessions) - 1, start_index - 1, -1):
            session = self.sessions[index]
            skills = session.get("skill_gains_points", {}) or {}
            skill_tt = session.get("skill_gains_tt", {}) or {}
            counts = session.get("skill_gain_events_by_skill", {}) or {}

            # Backward-compatible totals for sessions saved by older versions.
            skill_tt_total = float(session.get("skill_gain_tt_total", sum(float(v) for v in skill_tt.values())))
            skill_points_total = float(session.get("skill_gain_points_total", sum(float(v) for v in skills.values())))
            skill_gain_events_total = int(sum(int(v) for v in counts.values())) if counts else 0

            skill_rows = []
            for skill_name, point_gain in sorted(skills.items(), key=lambda item: item[0].lower()):
                tt_gain = float(skill_tt.get(skill_name, 0.0))
                gain_count = int(counts.get(skill_name, 0))
                count_text = f", {gain_count}x" if gain_count else ""
                skill_rows.append(
                    f"{skill_name}: +{float(point_gain):.4f} pts / +{tt_gain:.4f} TT "
                    f"({percent(tt_gain, skill_tt_total):.1f}% TT, {percent(gain_count, skill_gain_events_total):.1f}% msgs){count_text}"
                )

            skills_text = "; ".join(skill_rows[:4])
            if len(skill_rows) > 4:
                skills_text += f" ... +{len(skill_rows) - 4} more"

            mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip()
            ped_cycled = float(session.get('ped_cycled', 0.0))
            loot_ped = float(session.get('loot_ped_total', 0.0))
            loot_event_count = len(self.loot_events_for_session(session))
            cost_per_kill = ped_cycled / loot_event_count if loot_event_count else 0.0
            weapon = session.get("weapon", "")
            amplifier = session.get("amplifier", "")
            attachments = session.get("attachments", []) or []
            weapon_display = weapon
            if amplifier:
                weapon_display = f"{weapon or '-'} + {amplifier}"
            has_weapon_stats = weapon in WEAPONS
            dpp = hunting_setup_dpp(weapon, amplifier, attachments) if has_weapon_stats else 0.0
            efficiency = hunting_setup_efficiency(weapon)
            ped_per_hour = hunting_setup_ped_per_hour(weapon, amplifier, attachments) if has_weapon_stats else 0.0
            skill_tt_percent = percent(skill_tt_total, ped_cycled)
            avg_skill_tt_per_hour = (skill_tt_percent / 100.0) * ped_per_hour if ped_per_hour else 0.0
            avg_loss = avg_ped_loss_per_100(efficiency)
            skill_tt_minus_avg_loss = skill_tt_percent - avg_loss if avg_loss is not None else None
            defended_attacks = int(session.get("defended_attacks", session.get("jammed_attacks", 0)) or 0)
            missed_attacks = int(session.get("missed_attacks", 0) or 0)
            session_tags = ("analysis_valid",) if str(session.get("id", "") or "") in valid_analysis_ids else ()
            self.sessions_tree.insert("", "end", iid=f"session_{index}", tags=session_tags, values=(
                session.get("started_at", ""),
                session.get("ended_at", ""),
                weapon_display,
                mob,
                session.get("attacks_total", 0),
                defended_attacks,
                missed_attacks,
                f"{float(session.get('damage_total', 0.0)):.1f}",
                f"{ped_cycled:.4f}",
                f"{dpp:.3f}" if dpp else "",
                f"{efficiency:.1f}" if efficiency is not None else "",
                f"{ped_per_hour:.2f}" if ped_per_hour else "",
                f"{loot_ped:.4f}",
                f"{percent(loot_ped, ped_cycled):.2f}%",
                loot_event_count,
                f"{cost_per_kill:.6f}" if cost_per_kill else "",
                f"{skill_tt_total:.4f}",
                f"{skill_tt_percent:.2f}%",
                f"{avg_skill_tt_per_hour:.4f}" if avg_skill_tt_per_hour else "",
                f"{avg_loss:.2f}" if avg_loss is not None else "",
                f"{skill_tt_minus_avg_loss:.2f}" if skill_tt_minus_avg_loss is not None else "",
                f"{skill_points_total:.4f}",
                skill_gain_events_total,
                skills_text,
            ))
        self.apply_tree_sort(self.sessions_tree)

    def clear_sessions(self):
        if not messagebox.askyesno("Clear sessions", "Delete all saved session history?"):
            return
        self.sessions = []
        save_json(SESSIONS_FILE, self.sessions)
        self.refresh_sessions_table()
        self.show_session_details(None)

    def delete_selected_sessions(self):
        indices = self.selected_session_indices_from_table()
        if not indices:
            messagebox.showwarning("No sessions selected", "Select one or more sessions to delete first.")
            return

        selected_sessions = [self.sessions[index] for index in indices]
        if len(selected_sessions) == 1:
            session = selected_sessions[0]
            started = session.get("started_at", "")
            weapon = session.get("weapon", "") or "-"
            mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip() or "-"
            confirmation = (
                f"Delete the selected session?\n\n"
                f"Started: {started}\nWeapon: {weapon}\nMob: {mob}"
            )
        else:
            preview_lines = []
            for session in selected_sessions[:8]:
                started = session.get("started_at", "")
                weapon = session.get("weapon", "") or "-"
                mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip() or "-"
                preview_lines.append(f"• {started} | {weapon} | {mob}")
            if len(selected_sessions) > len(preview_lines):
                preview_lines.append(f"• ...and {len(selected_sessions) - len(preview_lines)} more")
            confirmation = (
                f"Delete {len(selected_sessions)} selected sessions?\n\n"
                + "\n".join(preview_lines)
            )

        if not messagebox.askyesno("Delete selected sessions", confirmation):
            return

        # Remove from highest index to lowest so earlier indices do not shift.
        for index in sorted(indices, reverse=True):
            del self.sessions[index]

        save_json(SESSIONS_FILE, self.sessions)
        self.refresh_sessions_table()
        self.show_session_details(None)
        self.refresh_loot_tab()

    def save_state(self, last_log_read_at=None):
        """Save app state without accidentally moving the log cutoff forward.

        last_log_read_at must mean: timestamp of the newest chat.log line that
        was actually read. It must not be set to current app time when the user
        only changes setup, browses for a log file, closes the app, or starts
        sync before any log line was processed.
        """
        chat_log_path = self.chat_log_path_var.get()
        path = Path(chat_log_path).expanduser() if chat_log_path else None
        file_size = 0
        file_mtime_ns = 0
        fingerprint = ""
        if path and path.exists():
            try:
                stat = path.stat()
                file_size = int(stat.st_size)
                file_mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
                if 0 <= int(self.log_offset) <= file_size:
                    fingerprint = log_resume_fingerprint(path, int(self.log_offset))
            except OSError:
                pass

        preserved_last_read_at = str(self.state.get("last_log_read_at", "") or "")
        if last_log_read_at is not None:
            preserved_last_read_at = str(last_log_read_at or "")
            self.last_log_read_at_var.set(preserved_last_read_at)

        self.state = {
            "chat_log_path": chat_log_path,
            "last_log_offset": int(self.log_offset),
            "last_log_read_at": preserved_last_read_at,
            "last_log_size": file_size,
            "last_log_mtime_ns": file_mtime_ns,
            "last_log_fingerprint": fingerprint,
            "weapon": self.weapon_var.get(),
            "amplifier": self.selected_amplifier(),
            "attachments": self.selected_attachments(),
            "mob": self.mob_var.get(),
            "maturity": self.maturity_var.get(),
            "count_hunting": bool(self.count_hunting_var.get()),
            "selected_hunting_setup": self.hunting_setup_name_var.get().strip(),
            "sync_start_mode": self.sync_start_mode_var.get(),
            "analysis_efficiency": self.analysis_efficiency_var.get(),
            "analysis_animal_looter": self.analysis_looter_vars["Animal"].get(),
            "analysis_robot_looter": self.analysis_looter_vars["Robot"].get(),
            "analysis_mutant_looter": self.analysis_looter_vars["Mutant"].get(),
        }
        save_json(TRACKER_STATE_FILE, self.state)

    def on_close(self):
        if self.monitoring:
            self.stop_sync()
        else:
            self.save_state()
            save_current_skills(self.current_skills)
            self.flush_event_text()
        self.root.destroy()


def main():
    root = tk.Tk()
    SkillTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
