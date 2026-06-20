import hashlib
import json
import os
import queue
import re
import threading
import time
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
IGNORED_LOOT_ITEMS = ("Universal Ammo", "Nanocube")


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
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


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


def ignored_loot_message(message: str) -> bool:
    lower_message = str(message).lower()
    return any(item.lower() in lower_message for item in IGNORED_LOOT_ITEMS)


def equipment_item_cost_per_shot_ped(item: dict) -> float:
    decay = item.get("decay") or 0.0
    ammo_burn = item.get("ammo_burn") or 0.0
    # Entropia data is usually in PEC-like fractional decay and ammo burn in ammo units.
    # PED cost = decay PEC / 100 + ammo units / 10000.
    return float(decay) / 100.0 + float(ammo_burn) / 10000.0


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
    jammed_attacks: int = 0
    attacks_total: int = 0
    damage_total: float = 0.0
    loot_ped_total: float = 0.0
    ped_cycled: float = 0.0
    events: list = field(default_factory=list)


class ChatLogParser:
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^]]*)\] \[([^]]*)\] (.*)$")
    gain_direct_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) ([A-Za-z][A-Za-z '\-]+)$")
    gain_experience_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) experience in your (.+?) skill$")
    improved_re = re.compile(r"^Your (.+?) has improved by ([0-9]+(?:\.[0-9]+)?)$")
    normal_damage_re = re.compile(r"^You inflicted ([0-9]+(?:\.[0-9]+)?) points of damage$")
    crit_damage_re = re.compile(r"^Critical hit - Additional damage! You inflicted ([0-9]+(?:\.[0-9]+)?) points of damage$")
    loot_re = re.compile(r"^You received .+ Value: ([0-9]+(?:\.[0-9]+)?) PED$")

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

        if message == "The target Jammed your attack":
            return {"type": "jammed", "timestamp": timestamp, "message": message}

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
            return {"type": "loot", "timestamp": timestamp, "value_ped": float(loot.group(1)), "message": message}

        return None


class SkillTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Entropia Skill Tracker")
        self.root.geometry("1280x820")

        self.current_skills = load_current_skills()
        self.state = load_json(TRACKER_STATE_FILE, {})
        self.sessions = load_json(SESSIONS_FILE, [])

        self.current_session: MonitorSession | None = None
        self.monitoring = False
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

        self.profession_var = tk.StringVar()
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

        self.weapon_var = tk.StringVar(value=self.state.get("weapon", ""))
        self.amplifier_var = tk.StringVar(value=self.state.get("amplifier", ""))
        saved_attachments = list(self.state.get("attachments", []) or [])[:3]
        while len(saved_attachments) < 3:
            saved_attachments.append("")
        self.attachment_vars = [tk.StringVar(value=value) for value in saved_attachments]
        self.mob_var = tk.StringVar(value=self.state.get("mob", ""))
        self.maturity_var = tk.StringVar(value=self.state.get("maturity", ""))
        self.count_hunting_var = tk.BooleanVar(value=bool(self.state.get("count_hunting", False)))
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

        self.create_ui()
        self.load_profession()
        self.refresh_hunting_info()
        self.refresh_sessions_table()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(500, self.monitor_tick)

    def create_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        self.profession_tab = ttk.Frame(notebook)
        self.monitor_tab = ttk.Frame(notebook)
        self.hunting_tab = ttk.Frame(notebook)
        self.sessions_tab = ttk.Frame(notebook)
        self.session_details_tab = ttk.Frame(notebook)

        notebook.add(self.monitor_tab, text="Live Monitor")
        notebook.add(self.hunting_tab, text="Hunting Setup")
        notebook.add(self.sessions_tab, text="Previous Sessions")
        notebook.add(self.session_details_tab, text="Session Details")
        notebook.add(self.profession_tab, text="Professions / Skills")

        self.create_monitor_tab()
        self.create_hunting_tab()
        self.create_sessions_tab()
        self.create_session_details_tab()
        self.create_profession_tab()

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
        numeric_text = text.rstrip("%").replace(",", "")
        try:
            return (0, float(numeric_text))
        except ValueError:
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
        ttk.Label(top, textvariable=self.monitor_status_var, font=("Arial", 11, "bold")).grid(row=0, column=5, padx=12)
        ttk.Label(top, textvariable=self.monitor_progress_var).grid(row=1, column=1, columnspan=5, sticky="w", padx=6, pady=(4, 0))
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

        event_frame = ttk.LabelFrame(self.monitor_tab, text="Recent parsed events", padding=6)
        event_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.event_text = tk.Text(event_frame, height=10, wrap="none")
        self.event_text.pack(fill="both", expand=True)

    def create_hunting_tab(self):
        frame = ttk.Frame(self.hunting_tab, padding=12)
        frame.pack(fill="x")

        ttk.Checkbutton(frame, text="Count hunting / PED cycled during sync", variable=self.count_hunting_var, command=self.save_state).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

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

        ttk.Button(frame, text="Save Hunting Setup", command=self.save_state).grid(row=12, column=1, sticky="w", padx=8, pady=12)
        frame.columnconfigure(1, weight=1)
        self.update_maturity_values()

        help_box = ttk.LabelFrame(self.hunting_tab, text="Counting rule", padding=10)
        help_box.pack(fill="x", padx=12, pady=10)
        ttk.Label(
            help_box,
            text=(
                "PED cycled increments on your attack events only: normal hit, critical hit, or 'The target Jammed your attack'.\n"
                "It ignores enemy attacks like 'The attack missed you' and 'You took damage'.\n"
                "Per attack cost is calculated from weapon, amplifier, and attachment decay / 100 + ammo_burn / 10000 PED."
            ),
            justify="left",
        ).pack(anchor="w")

    def create_sessions_tab(self):
        top = ttk.Frame(self.sessions_tab, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh", command=self.refresh_sessions_table).pack(side="left")
        ttk.Button(top, text="Delete Selected Session", command=self.delete_selected_session).pack(side="left", padx=6)
        ttk.Button(top, text="Clear Sessions", command=self.clear_sessions).pack(side="left", padx=6)

        columns = ("started", "ended", "weapon", "mob", "attacks", "damage", "ped", "loot", "loot_percent", "skill_tt", "skill_tt_percent", "skill_points", "skill_events", "skills")
        self.sessions_tree = ttk.Treeview(self.sessions_tab, columns=columns, show="headings", height=22)
        setup = [
            ("started", "Started", 155), ("ended", "Ended", 155), ("weapon", "Weapon", 200),
            ("mob", "Mob", 170), ("attacks", "Attacks", 75), ("damage", "Damage", 85),
            ("ped", "PED cycled", 95), ("loot", "Loot PED", 85),
            ("loot_percent", "Loot %", 75),
            ("skill_tt", "TT-equiv total", 105), ("skill_points", "Point total", 115),
            ("skill_tt_percent", "Skill TT %", 85),
            ("skill_events", "Skill gains", 90), ("skills", "Skills gained", 300),
        ]
        for col, title, width in setup:
            self.sessions_tree.heading(col, text=title)
            self.sessions_tree.column(col, width=width, anchor="center" if col not in ("weapon", "mob", "skills") else "w")
        self.make_tree_sortable(self.sessions_tree, {col: title for col, title, _ in setup})
        self.sessions_tree.pack(fill="both", expand=True, padx=10, pady=10)
        self.sessions_tree.bind("<<TreeviewSelect>>", self.on_session_selected)

    def create_session_details_tab(self):
        top = ttk.Frame(self.session_details_tab, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="Select a session in Previous Sessions to inspect exact skill gains, averages, hunting totals, and saved parsed events.",
        ).pack(anchor="w")

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

        events_frame = ttk.LabelFrame(self.session_details_tab, text="Saved parsed events (last 300 per session)", padding=6)
        events_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.session_detail_events_text = tk.Text(events_frame, height=10, wrap="none")
        self.session_detail_events_text.pack(fill="both", expand=True)

    def selected_session_index_from_table(self):
        selected = self.sessions_tree.selection()
        if not selected:
            return None
        iid = selected[0]
        try:
            index = int(iid.replace("session_", ""))
        except ValueError:
            return None
        return index if 0 <= index < len(self.sessions) else None

    def selected_session_from_table(self):
        index = self.selected_session_index_from_table()
        if index is None:
            return None
        return self.sessions[index]

    def on_session_selected(self, event=None):
        self.show_session_details(self.selected_session_from_table())

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
        skill_tt_percent = percent(skill_tt_total, ped_cycled)
        skill_messages_per_attack = percent(skill_events_total, session.get('attacks_total', 0))
        attachments = ", ".join(session.get("attachments", []) or []) or "-"

        self.session_detail_summary_var.set(
            f"Started: {session.get('started_at', '')} | Ended: {session.get('ended_at', '')}\n"
            f"Log: {session.get('chat_log_path', '')}\n"
            f"Weapon: {session.get('weapon', '-') or '-'} | Amp: {session.get('amplifier', '') or '-'} | Attachments: {attachments}\n"
            f"Mob: {mob} | Count hunting: {bool(session.get('count_hunting', False))}\n"
            f"Attacks: {session.get('attacks_total', 0)} "
            f"(hits {session.get('normal_hits', 0)}, crits {session.get('critical_hits', 0)}, jammed {session.get('jammed_attacks', 0)}) | "
            f"Damage: {float(session.get('damage_total', 0.0)):.1f} | PED cycled: {ped_cycled:.4f} | "
            f"Loot: {loot_ped:.4f} PED ({loot_percent:.2f}%)\n"
            f"Skill gain messages: {skill_events_total} | Point total: {skill_points_total:.4f} | "
            f"TT-equivalent total: {skill_tt_total:.4f} ({skill_tt_percent:.2f}% of cycled) | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}% | "
            f"Avg/message: {avg_points:.6f} points / {avg_tt:.6f} TT"
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
            elif etype == "jammed":
                line = f"{timestamp} | jammed | {event.get('message', '')}"
            elif etype == "loot":
                line = f"{timestamp} | loot | +{float(event.get('value_ped', 0.0)):.4f} PED | {event.get('message', '')}"
            else:
                line = str(event)
            self.session_detail_events_text.insert("end", line + "\n")
        self.session_detail_events_text.see("1.0")

    def browse_chat_log(self):
        filename = filedialog.askopenfilename(
            title="Choose Entropia chat.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if filename:
            self.chat_log_path_var.set(filename)
            self.save_state()

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
            profession_gain = skill_gain * data["weight"] / 100.0
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

        # Resume only when the saved offset belongs to the same unchanged log.
        # Size checks alone are not enough: chat.log can be cleared/replaced and
        # later grow past the old offset, which would make us skip valid data.
        previous_state = dict(self.state)
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
            # If the log was cleared/replaced, we must scan from byte 0, but we
            # should not re-apply older lines. Only lines whose chat timestamp is
            # at/after the previous last_log_read_at are processed. With no prior
            # state, the cutoff is None and the whole file is imported.
            self.log_time_cutoff_at = parse_iso_datetime(saved_last_read_at) if TRACKER_STATE_FILE.exists() else None
            if self.log_time_cutoff_at is None:
                resume_message = "Started sync session from beginning of log"
            else:
                resume_message = f"Started sync session from beginning of log; skipping lines before {saved_last_read_at}"

        self.log_offset = start_offset
        self.save_state()

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
        )
        self.monitoring = True
        self.monitor_status_var.set("Syncing")
        self.append_event(resume_message)
        self.update_session_summary()
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
        self.process_reader_queue(max_batches=1_000_000)

        self.current_session.ended_at = now_iso()
        self.current_session.end_offset = self.log_offset
        self.sessions.append(asdict(self.current_session))
        save_json(SESSIONS_FILE, self.sessions)
        self.append_event("Stopped sync session and saved it")
        self.current_session = None
        self.log_time_cutoff_at = None
        self.monitoring = False
        self.reader_active = False
        self.reader_done_pending = False
        self.monitor_status_var.set("Stopped")
        self.monitor_progress_var.set("")
        self.session_summary_var.set("No active session")
        self.refresh_sessions_table()
        self.save_state()

    def monitor_tick(self):
        if self.monitoring:
            self.process_reader_queue(max_batches=3)
            if not self.reader_active and not self.reader_done_pending:
                self.start_log_reader_until_current_eof()
        self.root.after(200, self.monitor_tick)

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

    def process_reader_queue(self, max_batches=3):
        """Apply parsed worker batches on the Tkinter thread."""
        processed_batches = 0
        refresh_needed = False
        while processed_batches < max_batches:
            try:
                item = self.reader_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "progress":
                _, lines_seen, current_offset, end_offset = item
                percent = (current_offset / end_offset * 100.0) if end_offset else 100.0
                self.monitor_progress_var.set(f"Scanning log: {percent:.1f}% | lines checked: {lines_seen:,} | offset {current_offset:,}/{end_offset:,}")
                continue

            if kind == "batch":
                _, events, skipped_by_time, newest_iso, batch_end_offset = item
                if skipped_by_time:
                    self.append_event(f"Skipped {skipped_by_time} old parsed events before last_log_read_at")
                for event in events:
                    self.apply_event(event)
                self.log_offset = int(batch_end_offset)
                if newest_iso:
                    self.save_state(last_log_read_at=newest_iso)
                else:
                    self.save_state()
                processed_batches += 1
                self.reader_processed_batches += 1
                refresh_needed = True
                self.monitor_progress_var.set(f"Applied {len(events):,} parsed events | offset {self.log_offset:,}")
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
                continue

        if refresh_needed:
            save_current_skills(self.current_skills)
            self.refresh_session_skill_tree()
            self.update_session_summary()
            self.load_profession_keep_selection()

        if self.reader_done_pending and not self.reader_active:
            # Finalize only after all earlier queued batches were already applied.
            # If there are still queued batches, wait for the next tick.
            has_pending_batch = False
            try:
                # Peek is not supported, so use queue size as a cheap signal. If
                # anything remains, it will be processed before finalization.
                has_pending_batch = self.reader_queue.qsize() > 0
            except NotImplementedError:
                has_pending_batch = False
            if not has_pending_batch:
                if self.reader_final_offset is not None:
                    self.log_offset = int(self.reader_final_offset)
                if self.reader_final_last_read_at:
                    self.save_state(last_log_read_at=self.reader_final_last_read_at)
                else:
                    self.save_state()
                self.reader_done_pending = False
                self.reader_final_last_read_at = ""
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

        save_current_skills(self.current_skills)
        last_read_at = newest_line_at.isoformat(timespec="seconds") if newest_line_at else None
        self.save_state(last_log_read_at=last_read_at)
        self.refresh_session_skill_tree()
        self.update_session_summary()
        self.load_profession_keep_selection()

    def apply_event(self, event):
        if self.current_session is None:
            return
        session = self.current_session
        session.events.append(event)
        if len(session.events) > 300:
            session.events = session.events[-300:]

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
            self.current_skills[skill] = new_points
            session.skill_gains_points[skill] = session.skill_gains_points.get(skill, 0.0) + point_gain
            session.skill_gains_tt[skill] = session.skill_gains_tt.get(skill, 0.0) + tt_gain
            session.skill_gain_events_by_skill[skill] = session.skill_gain_events_by_skill.get(skill, 0) + 1
            session.skill_gain_points_total += point_gain
            session.skill_gain_tt_total += tt_gain
            self.append_event(f"{event['timestamp']} skill +{point_gain:.4f} pts: {skill} ({old_points:.4f} -> {new_points:.4f})")
        elif event_type in ("normal_hit", "crit", "jammed"):
            session.attacks_total += 1
            if event_type == "normal_hit":
                session.normal_hits += 1
                session.damage_total += float(event["damage"])
                self.append_event(f"{event['timestamp']} hit {event['damage']:.1f}")
            elif event_type == "crit":
                session.critical_hits += 1
                session.damage_total += float(event["damage"])
                self.append_event(f"{event['timestamp']} CRIT {event['damage']:.1f}")
            else:
                session.jammed_attacks += 1
                self.append_event(f"{event['timestamp']} jammed/missed attack")
            if session.count_hunting:
                session.ped_cycled += hunting_setup_cost_per_shot_ped(
                    session.weapon,
                    session.amplifier,
                    session.attachments,
                )
        elif event_type == "loot":
            session.loot_ped_total += float(event["value_ped"])
            self.append_event(f"{event['timestamp']} loot +{event['value_ped']:.4f} PED")

        session.end_offset = self.log_offset
        session.total_profession_gain_by_profession = self.calculate_profession_gains_for_session(session)

    def calculate_profession_gains_for_session(self, session: MonitorSession):
        gains = {}
        for profession_name, profession in PROFESSIONS.items():
            total = 0.0
            for skill, weight in profession["skills"].items():
                total += session.skill_gains_points.get(skill, 0.0) * float(weight) / 100.0
            if total:
                gains[profession_name] = total
        return gains

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
        self.session_summary_var.set(
            f"Started: {s.started_at} | Weapon: {s.weapon or '-'} | Amp: {s.amplifier or '-'} | Attachments: {attachments}\n"
            f"Mob: {s.mob or '-'} {s.maturity or ''}\n"
            f"Attacks: {s.attacks_total} (hits {s.normal_hits}, crits {s.critical_hits}, jammed {s.jammed_attacks}) | "
            f"Damage: {s.damage_total:.1f} | PED cycled: {s.ped_cycled:.4f} | Loot: {s.loot_ped_total:.4f} PED ({loot_percent:.2f}%)\n"
            f"Skill gains: {total_skill_gain_events} messages | Point total: {s.skill_gain_points_total:.4f} | "
            f"TT-equivalent total: {s.skill_gain_tt_total:.4f} ({skill_tt_percent:.2f}% of cycled) | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}%\n"
            f"Top skills: {top_skills_text}\n"
            f"Top profession gains: {top_text}"
        )

    def append_event(self, text: str):
        self.event_text.insert("end", text + "\n")
        self.event_text.see("end")

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
            self.sessions_tree.insert("", "end", iid=f"session_{index}", values=(
                session.get("started_at", ""),
                session.get("ended_at", ""),
                session.get("weapon", ""),
                mob,
                session.get("attacks_total", 0),
                f"{float(session.get('damage_total', 0.0)):.1f}",
                f"{ped_cycled:.4f}",
                f"{loot_ped:.4f}",
                f"{percent(loot_ped, ped_cycled):.2f}%",
                f"{skill_tt_total:.4f}",
                f"{percent(skill_tt_total, ped_cycled):.2f}%",
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

    def delete_selected_session(self):
        index = self.selected_session_index_from_table()
        if index is None:
            messagebox.showwarning("No session selected", "Select a session to delete first.")
            return

        session = self.sessions[index]
        started = session.get("started_at", "")
        weapon = session.get("weapon", "") or "-"
        mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip() or "-"
        if not messagebox.askyesno(
            "Delete selected session",
            f"Delete session from {started}?\n\nWeapon: {weapon}\nMob: {mob}",
        ):
            return

        del self.sessions[index]
        save_json(SESSIONS_FILE, self.sessions)
        self.refresh_sessions_table()
        self.show_session_details(None)

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
        }
        save_json(TRACKER_STATE_FILE, self.state)

    def on_close(self):
        if self.monitoring:
            self.stop_sync()
        else:
            self.save_state()
            save_current_skills(self.current_skills)
        self.root.destroy()


def main():
    root = tk.Tk()
    SkillTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
