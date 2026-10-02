import hashlib
import json
import math
import os
import queue
import re
import threading
import time
from collections import deque, OrderedDict
import tkinter as tk
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import base64

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
FAVORITE_MOBS_FILE = Path("favorite_mobs.json")
LOOT_MARKUPS_FILE = Path("loot_markups.json")
MARKET_DATA_FILE = Path("market_data.json")
SKILLS_DATA_FILE = Path("skills.json")
PROFESSIONS_DATA_FILE = Path("professions.json")
IGNORED_LOOT_ITEMS = ("Universal Ammo", "Nanocube")
# Some stackables do not print quantity in chat.log. Derive count from TT value.
STACKABLE_ITEM_PED_VALUE = {"Shrapnel": 0.0001}
LOOT_TRACKER_GRAPH_VERSION = "loot-local-time-mu-dpp-v13"

# Presentation preferences are separate from saved sessions and equipment.
UI_STYLES = ("Compact", "Command")
LEGACY_UI_COLOR_SCHEMES = {"Compact": "Light", "Command": "Dark"}
UI_COLOR_SCHEMES = {
    "Light": {
        "background": "#f3f5f8", "surface": "#ffffff", "ink": "#1d2b3a",
        "muted": "#637286", "accent": "#176a78", "border": "#d7dfe9",
        "hover": "#eaf5f5", "pressed": "#dcebed", "disabled": "#8793a0",
        "heading": "#eaf0f3", "scrollbar": "#dce3e8", "field": "#ffffff",
        "selection": "#176a78", "selection_ink": "#ffffff", "axis": "#8a95a3",
        "grid": "#e6e9ee", "positive": "#15803d", "valid_session": "#dff3df",
        "selection_preview": "#dcebed",
        "series": ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#65a30d", "#be123c", "#0284c7", "#ca8a04", "#475569", "#7c3aed"],
    },
    "Dark": {
        "background": "#10151e", "surface": "#191f2b", "ink": "#edf1f8",
        "muted": "#a8b5c8", "accent": "#8cd3bb", "border": "#344154",
        "hover": "#243d38", "pressed": "#305449", "disabled": "#8591a4",
        "heading": "#243040", "scrollbar": "#43516a", "field": "#111824",
        "selection": "#305449", "selection_ink": "#edf1f8", "axis": "#9ba9bd",
        "grid": "#2d3748", "positive": "#8cd3bb", "valid_session": "#243d38",
        "selection_preview": "#305449",
        "series": ["#82b8ff", "#ff9191", "#8cd3bb", "#c49bff", "#ffb575", "#79d5e1", "#badc7b", "#ff9bc4", "#6ed0ff", "#f3d273", "#c2cfdf", "#aaa5ff"],
    },
}


def saved_ui_appearance(state):
    """Keep legacy Compact/light and Command/dark preferences on upgrade."""
    legacy = state.get("ui_theme")
    if not isinstance(legacy, str) or legacy not in UI_STYLES:
        legacy = "Compact"
    style = state.get("ui_style", legacy)
    if not isinstance(style, str) or style not in UI_STYLES:
        style = legacy
    scheme = state.get("ui_color_scheme", LEGACY_UI_COLOR_SCHEMES[legacy])
    if not isinstance(scheme, str) or scheme not in UI_COLOR_SCHEMES:
        scheme = LEGACY_UI_COLOR_SCHEMES[legacy]
    return style, scheme


LOOT_EVENT_GROUPING_VERSION = 2

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


# Cumulative raw skill TT/PES curve, 0..20,000 skill points.
#
# Source model: the official-wiki chip-in curve as tracked by EntropiaOrme.
# The curve matches Entropia Central's visible calculator anchor at 5,000
# points (149.86 PED) and its 100 PED worked example.  The historical table
# previously used by this tracker was effectively filled-implant/post-
# extraction value and therefore understated raw skill value.
#
# The 20,001 integer-point anchors are stored losslessly as second differences
# of TT cents. Most second differences are -1/0/+1, so a compact 2-bit stream
# plus a tiny escape stream keeps this tracker self-contained.
SKILL_TT_CURVE_MAX_POINTS = 20_000
SKILL_TT_CURVE_VERSION = "official-wiki-raw-tt-0-20000-v1"
_SKILL_TT_CURVE_POINT_COUNT = SKILL_TT_CURVE_MAX_POINTS + 1
_SKILL_TT_CURVE_CODE_BYTES = (_SKILL_TT_CURVE_POINT_COUNT + 3) // 4
_SKILL_TT_CURVE_PAYLOAD_B64 = (
    'BgGAABgABgAAYAAYABgABgAGAAYABgAGABgAGABgAYAGABgAYAGAGABgBgAYAYAYAYAYAYAYBgBgBgGAGAYABgGAYGAYGAYGAYGBgGBgYGBgYGGBgYGGBhgYYYGGGGGGGGGGGGYYZhhmGYZhmGZhmYZmYZmZhmZmZmYZmZmZmZmZmZhmZmZmZmZmZhmZmYZmZhmZmGZhmYZhmGYZhmGYYZhhmGGGGYYYYYYYYYYYYYYGGGGGGGGBhhhhhgYYYYYGGGGGGGGGGGGYYYYZhhhmGYZhmGYZmGZmGZmZhmZmZmSZmZkmZkmSZJkmSZJJJJJJJJJJJCSSQkkJJJCQkJJCSQkJJCSQkkkJJCSSSSSSSSSSSSZJJkkmSZkmSZkmZmSZmZmSZmZmZmZmZmZhmZmZmGZmZmYZmZmGZmZmYZmZmZmZmZmZmZmZmSZmZJmSZkmSZJkkkkkkkkkJJCQkJAkJAJAJAJAAkAACQAAAAAAAAAAAAAAAGAAAABgAAAGAAAAYAAAAGAAAAAAAAAAAAAACQAAAJAAJAAkAJAJAJAkJAkCQkJCQkkJCSQkkJJJCSSSQkkkkJJJJJCSSQkkJJCSQkJCQkCQkCQJAJAJAAkAAJAAAAAAAAAAGAABgAYBgGBgYGBhhhmGGYZhmZmGZmZkmZmSZmSZJmSZJkmZJkmZJmSZkmZmZmZmZmZhmYZmGYYYYYYYYGBhgGBgGAYAYAGAAYAABgAAAYAAAAAAAGAAAAAAAAAAAAAGAAAAAGAAAYAAYAYAYBgGBgYGGGGGGGYZmZmZmZkmSSSSSQkCQJAAkAAAAAAABgAGAGAYBgYGGBhhhhhhhhhmGGGGGGGGYGGGBgYYBgYBgBgABgAAAAAAAAACQAJAJAkCQkkJJJJCZJJkmSZkmZkmZmZmSZmZmZmZmZmSZmZkmZJmSZJkkkkkkJJAkJAJAAkAAAAAAYABgGBhgYZhmZmZkkkkkJAkACQAAABgABgGBgYYYYYZhmGZmYZmZmZmZmZmGZmYZhmGGYYGGBgYBgAYAAAAAAACQAkCQkJJJJkkmZmZmZmGYZhhmGGBhhgYGBhgGBgYGBgYBgYYGGBhhhhhmGYZmZmZmSSZJJCQkAJAAAAAYAGBgZhmZmSZCSQCQAAAGAGBhhmGZkmSQkkCQAkAAAAAYAABgBgAYAYAYAYABgAAAAAAAACQCQkJJJJmZmZhmBgYYABgAAAAkAJAkJJJJJkmZmZmZhmGYZhhhhhhhgYYYYYcDAYGBgYYGGGYZmZmZkmSQkCQAAAAAYBhhmZmZCQkAAAAGBhhmZkkkJAAAABgYGYZmSZJCQJAAAAAABgBgGBhhgYZhhhhmGGGGGGBhgGAYABgAACQAJCSSSZmZhmBgGAAAACQJJJJmYZhhgGAAGAkAAJAkJJJJJJkmZJmZkmZkmSZJJJJCQJAAAAAAGBhhmZkkJACWABgZmZJCQAAGBhmSSQAAAGGGZJJAkAYAYZhJkkJAAAAABgYYZhmZmZJkmSSSSSSSZJkmSZmYZhhgYBgAAAJAkkmZhhgYAAAkJJmZhhgAAAAkkmSGYYBgAAAAJCQkkmZkmGZmYZhmYZmZhJmZJJJAkAAAAYGGZmSQJAYBhmZCQABhhkkAAAYZkkAAGGZJAAAGGSSQABgZmSQkABgYYZJkJAkAAAGAGBgYGGGGBhhgYBgAYAACQJCSZmGGAB0AJJmYYGAJCSZmBgAJCZIYGAAJCSYZgGAAkCSSZmGYGBgAYAAAAAAAYAABgGBhmGSSQkAAGGGSSQAYGZkAAGGSQAGGSQBhmQlgZmQAYZCQGGZAAAZkkAGGZkAAAZhJJAAAGBmGSZJJAkCQAkACQJAkkkmZmGAYAAkJmZgBpAkmGAACZmGACSZgAACZgYAkmZgAAkmZgAAAnBgAGABgYGGZmSSQAAYGZJAAAZkkABmZAAwAAYZmQAAGZkJBhmZABhkkAYZJAYZkAAZkAAZmQAYZJAGGZJABhmSQABhmZJAkAYAYYZmZmSZJkmSZmZmYYYBgACQkmZgYAJCZhgACSZgAAJmGACSYYAAmZgAAkhhgAkmZgYAJCSZhgYAAAAAJAkJAkCQAAlgAGGYSSQABhmQkGBkkAGZJBhkkYZkBmSWGSRhJAZkAGQAGZBhkABkkYZkAGZAABkkABhmQkAYGGZkkJACQAAAACQAkJJJhmAAACSYYACSYYCSZgAJmAAmGAkhgCZgAkgaQmAAJmAAmYYJCZgGkCZmGAAAAkJJkmSZkmSSQCQBgYZJAABmSQYZJBhJBhJBmQBkAZLhkBkuGQZJZkBkASRhAGQASRmQBkAGQAZkAZkLgZkAAGEkkAAAYGGGGYYYYYAAAAmZmAAJmYAkmAAmAAmGkmAJgAmAJgAmAmYJmAmAAhiSGJIYCZgAkgYkJhgAJJIZgGAAAAAAAYAYZmSQAYZJAZmQYSRhJYSWEAEAEAZBkGQGQSWQBAZBJZAZGQBAZBkGQBAGQGQZkGZBmQBmQAZkAAYZJAJBgAGABgAAkJJmAACZgAJgAJgCYAmAJgJgmaSAmAgAgCAJghpIJgCHSAJgIYmYJmAmGkIYCSGAAJJmGAYAABgAYYZkJBhmQAZAAZBmQZAZAQBkZBAGWQZZAQZEAQQEBAQQEBAwQAZBkAGSWBkkAGBmGZmGYGAAJmYAkgAJpJkJCQkJCSZmGBgJJIYAJmAAmGJIACYCZiSACGkgCYCYCYAgAmCYdIaSAJmkmAJh0JgAJmAAmYYAAJJJmZmGZmZkkAABhkJGGZBhkBmQZkZkGQGRmRkGQZBAGWQGWQEBlkBuEBAZZlkBlhBkBABAGQZABAAZAGZABmQkGBhmSSSQkkkmYYAAJIYAkgdCB0mAmAmAIAmJgCAICYIAmmAIJgmCAJpmmaZpmJgCACACYCYaSYACYYAJJmGGAGABgGGZkJGBkkGZABJYQAQBAGRkGRkZBlkG4RkQBGRAFkBEuRkQG5ARkEGRkZYRkGQZGQBABABJGEJYZkABhmZJJJJJJhhgAmYYkmAmYkgJgJiYCAICAnSCAgICCaYmmCAggICCAgIJpgIJpmmCYJgmAmAmAJhpJmACSZmGGBgYZmSQBhkAGQBkAQBkZAQEBlkZEARAQQEQEEEEZZQEEQEbkQEQFkZQEEBEARLkEBlkGQS4QBkGEAYQlgYSZAkCQJJmGAJIYJmAhiYCYmCYmIAggIIICCCCCCdpppoCCICIIICmmmIJ2CCAggIJiHZpgIdIAIaSYACZhgAAAAAYGEkAGZBmRmWEGRkG4RLkbkEEEEEFkRuWUEFlBBEbuREEERBBEEEQQRlARlAbkEMGEJBgGYYZgGAkmB0mAIaSCYCAmmCaYICDYCYCAJiYmCaYCJgggJ2CCAgggmgICICCAiaYmmJiAmmJggCAmCACYmYCYACZgAAkkmZmZJCQGGZAZkASWEBLhBkZBAQQEBEBBBBAWW5BEBZQEQQRuWW5G5ZEZZZZZEBBBlkEBAZElmRkAZAGZABhmQkCQJCZmGAkhiSHSYIaYCaYJpggmgCmJ2J2J2IImggggppoCmInYgKaaYggmgJ2CAnSJgIAgCYCZiQhgACQmSZJAkYGZAYQBkGQGWQEBBAQFkZQEQQQRBG7kRsBblBBQOREERA5EQQWUBZZQEbkEEGWRkZGQEAEAZJYZJABgYYGAAAJmAJgAgCYmCaYJ2AiAgidiCCIIKaCIIgoCgiIIgoIKdoIgiCCnYggiaAiaYgCJiYJgmCZgkgYkJmZhmZkkAYSRhAZAZZAQEEGUBBG5EbluUEOREFBERBQWwREFEEWwOUFu5bAUEERuRGUBBGRAZZGQGRklhJBhhJJJCZmYAJmAmCYCaYCJiAggiApoIIiCIg2IiIgogoKCgoKCKCgigiIgoIiCnYiCIJ2mnSICaYmAgAgAmYCQmZmZJJAGZAGQZAQBBAQQQFllBBEbBBQUEREREREUFBREUE5RERQTlBQUFBERBQORBEEEyYmmICIIiCgpyCoKIoooMiiiMNyiMKKKIsAJmACYAmYkgdmAmAmAmAIAmCYCYCYJgJgCACACGkgAhgmYJIYJIYAkhgAAJJhhhgGAYBhhmZJAAYZAABJAYQAZAGQGQBkZlhAZGQBAZBAGRkEuEBAEBkZAEBLhAEAEAZGEAGQGQAGSWBkJGBmZJAAAAAGAAAJCZmGAJJgAJmAmYCYAIdJgmAIdIAgCHZiYCYmAhpgJiYAgCACAJgmACACYAIYJIYAmZgACSZmGGBhgYYZkkAABmQAGZBhJGZBkuEAZBJZlm4QGWZZBkZBAEBAEEAbhBkZBkEuEBAGQZBmWEAGQBkABkkGGZJAABgYYYGGAAAAmZgAJmACYAIYJgCYJgCAJ0IJgmJgJpgIdgIAgmCHZpgCAgCHSAmCYAmAmAJmCZhiQmBgJCSSZhmZmZJALgGSQAZC4ZAGQBABLhAS4QZBkZBAZEAQEGWQEG4QQBBAbhBkZGQZYQZAQBJZkZkGZAZkBhkJBhhkmQkJCQmZmGACZmAJmAJgCYJmJgCAIAgIAgJpgmJiHYCAgICAgICAmmCYmCYmCYJgJgmAJgJmAmYAJmGAACSSSZJJAkAYGSQGGQGZAZAEuEAQBAEBAZZBlkBGRkZZBBlkEG4RLkEBBkQBBlkBAEGQEAQAQAQAZABkkGGZAkBgBgYAYAJCZhgkmACYAmAmCYAgJiYJiYmCAgIJpggIJ2dgmgCCAgmmJggICAmmAmJgmCZ0mAmACYaQmGAAJCSZJkkkAAGGQAGZBmQGQElmWZZAQGWQQEBARkZZEBARAbkEZEBARAbkEBAQQGWQbhAEGQGQZAZkGZAGZkABhhmZmZmGAAAmYACYAmAJgmAgCaYCaYmCAggCmCAiaYgIICICApgggIIJpidIICdImAgCAhpmJIAmAAmAdAmZhgYGBhhJJAYZJGZAGQZuGWZZAQEBAQEEBBAQQQZQEEEG5EBEBBBARAQQEEGWRlkBBkEAQBAGQZAAQABkkAGBhmZmGGAAAmYAmYCYAmCGmdIJiYmmApKYhx2mCIcCCCCCCCCCJiCCAiAggmh2CCYgCAgJiYCYJhpmACZgAJJmZmYZkkkAYZAAZBhAElkAQZGQQEBBAQbkbmwG5ZZEEEZQEbkQEQbkQFkEEG5ARLkGRlmWQBAEuBAAZAAYZJACQAAkkmYAJIYmYCYJgJpnSJggIICCApiaAgpgpiCJoIIIIgggiApiCCCCdidgIgCCAmmAgCAJgmYkmAAmZhgAAGAGBkkAGZBhAGQEAQBAQEGWRkQZQEEEEEQEQbAQWWUBEEbkQQQQRARlAFkZZGRAEGRkBkZuBkBmQABmSSQkJJIYYAmYAIaSAIdnSCaYJ2CdgiaaAgiCCCIIKadiIIIgiCICgIgiApiCCCCAgggICAgCCYJnSYCSAAkmBgBgAGBmZAAZAAQBkGQEBAZZBBAQQQEQQbLARBBEEbluWUEEQORBEG5QEbkSwQBZEuRkG4QGWEAZAGQABhJCQCQCSZmACSAJgCYmAnSCaYmmICJoCmJwIidiIIKCcIKdoDYiCIggiICggnAiA0piaHYnSCYmJmmYJgAJgHQmSZmGZAkGBkAZkYQZAQBBlkEG4WWWRBBEEEEQRGUEQRBZQQREEFuRBEEEbkRkQQEEZGRkb+QZAEAEkH5kAAB+AGAAJmBpJiSHSCYIAgnZ2mCCICIIKadoJwiCIgpogiIgiIKCJwiCIndiIIIgKaYiYnYCCAIJgmAmAJh0CSGGBgYYZJAASQZkZuGWQEEBuQbuFkQQRsBEEFBBEFlEEFBGwRBEEQ5BbkQRBu5u5BARkS5GQGRkGEBhJBgZJmQkmSBgCYYmYJgIA/Z2CCdgidg2cCgKd2IgoIg2IiIicIpoiCP52gp2KAoIPyApoCJ2CCCaTSAgJgJgCYYCSSBmYZkm/gZABkBJZAQG5uQQbkbkQQRBEEbu5EQW5QREbsDkW5FuUEFlBEEblkRAQQbkZBAEEuGRmQGZAABhhmGAfiZmJIYmAgJiYnSmJiICImggpogicgiIgoKCNiIoIiIiIg2icIiCgiCIgiCCICmmJ0pggCAgAgCYaQmGAGkYAGGSQYb+ZBL+4QEEBuRBBAREEERu5FlBEQUERGwRZQUERBbuTmxAUEEOQQRlluEZZuQEASRkAGZAAGGGfgYAmYdCACAmJggJ2IdoCDYIKCCggoIpoiIg2iIgop3CI2KCNoIoKbYnCgKCIIggg0giAnYIaACAmAJhpCSYYYYZkkBgQBkAbhAQEBBuRARuREEERu7kRGwRZRBOREREFsbAUQREQREEFBuUBEDhG4RuEBAZBkAEkAZhkmQmZhgJIGmHZiYIAidnYgnAiCIIpoIiIiCiciC2IiKCpqdwoKIiIpwiIiIgoIgiIIgnaAgggCmAgmYmAJgHQmSGGZkkABkAGQZAQG5lllkEQERAW5EQ5EFBEWUFBORZWwFERBQREWRQQWwFlBBG5EEsBBkQBuZASWBkAGGGZmYGAkmACHZiYCA/dInYNndp2nA2KCIiIiIpw2gwigooI2inJyIiNigjYjYiCgg2ImgggiYgICCGIAJiQgHQJmZJmSQGZkGS4QZBLkEG5GUBZbluTkFBEOTkWwQwQ5REREFEFBQW7uRQQW5SxuRBBuWQQS5uZGQAQAGZJAAAAfIN+e4OCgoJ+iH5+gn5+goKCfoKCgn5+fn6CgoKCgoKCin59foKCgoKCfn5+fn5+foKCgoKCgoJ+fn5+goKCgoKCgoKCgoJ+fn5+goKCgqR+fn5+fn55foKCfn5+goKCgn5+fn6CgoKCgoJ+goKCfn5+goKCfn5+goJ+fn5+foKCgoKCgoKCgn5+foKCgn5+fn5+foKCgoKCgoKCgoJ+fn5+fn5+fn5+foKCgoJ+gn6CfoJ+fn5+fn5+fn5+goKCgoKCgoKCgoKCgn6Cfn5+fn5+fn5+fn6Cfn5+foJ+fn6CfoKCgoKCgoKCgoKCgoKCgoKCfoJ+fn5+fn6CfoKCfoKCgoKCgoKCgoKCgoJ+gn5+fn5+fn5+fn5+fn6CgoKCgoKCgoKCgoKCgn5+fn5+fn5+fn5+goKCgoKCgoKCfn6Cfn5+fn5+fn5+fn5+fn5+fn5+fn6CgoKCgoKCgoKCgoKCgoKCgoKCgg=='
)


def _decode_skill_tt_curve() -> tuple[float, ...]:
    packed = base64.b64decode(_SKILL_TT_CURVE_PAYLOAD_B64)
    codes = packed[:_SKILL_TT_CURVE_CODE_BYTES]
    extras = packed[_SKILL_TT_CURVE_CODE_BYTES:]
    extra_index = 0
    first_difference_cents = 0
    value_cents = 0
    values = []

    for index in range(_SKILL_TT_CURVE_POINT_COUNT):
        byte = codes[index // 4]
        shift = 6 - 2 * (index % 4)
        code = (byte >> shift) & 0b11
        if code == 0:
            second_difference = 0
        elif code == 1:
            second_difference = 1
        elif code == 2:
            second_difference = -1
        else:
            if extra_index >= len(extras):
                raise RuntimeError("Corrupt embedded skill TT curve (missing escape value).")
            second_difference = int(extras[extra_index]) - 128
            extra_index += 1

        first_difference_cents += second_difference
        value_cents += first_difference_cents
        values.append(value_cents / 100.0)

    if extra_index != len(extras):
        raise RuntimeError("Corrupt embedded skill TT curve (unused escape values).")

    # Pinned anchors make accidental edits/corruption fail immediately.
    expected = {
        0: 0.0,
        100: 0.12,
        1_000: 3.42,
        5_000: 149.86,
        10_000: 2597.01,
        14_750: 7944.19,
        20_000: 13381.54,
    }
    for point, tt_value in expected.items():
        if abs(values[point] - tt_value) > 1e-9:
            raise RuntimeError(
                f"Corrupt embedded skill TT curve at {point:,}: "
                f"{values[point]:.6f} != {tt_value:.6f}"
            )
    return tuple(values)


_SKILL_TT_CURVE = _decode_skill_tt_curve()


def skill_tt_value(skill_points: float) -> float:
    """Return cumulative raw skill TT-equivalent (PED) at ``skill_points``.

    The embedded official-wiki curve contains every integer point from 0 to
    20,000. Fractional skill points are linearly interpolated between adjacent
    anchors, matching the reference calculator's interpolation approach.

    Entropia Central has a newer private extension through 100,318 points, but
    its >20,000 anchors/formula are not publicly exposed.  Refuse to silently
    invent values beyond the verified range instead of using the old linear
    extrapolation.
    """
    skill_points = float(skill_points)
    if not (skill_points >= 0.0):
        raise ValueError("Skill points must be a non-negative finite number.")
    if skill_points == float("inf"):
        raise ValueError("Skill points must be a non-negative finite number.")
    if skill_points > SKILL_TT_CURVE_MAX_POINTS:
        raise ValueError(
            f"Verified skill TT curve currently supports 0..{SKILL_TT_CURVE_MAX_POINTS:,} points; "
            f"got {skill_points:,.4f}. Entropia Central extends beyond this range, "
            "but its >20,000 curve is not publicly available for an exact offline calculation."
        )

    lower = int(skill_points)
    if lower >= SKILL_TT_CURVE_MAX_POINTS:
        return _SKILL_TT_CURVE[SKILL_TT_CURVE_MAX_POINTS]
    fraction = skill_points - lower
    lower_value = _SKILL_TT_CURVE[lower]
    upper_value = _SKILL_TT_CURVE[lower + 1]
    return lower_value + fraction * (upper_value - lower_value)


def find_skill_after_tt_delta(current_points: float, delta_tt: float) -> float:
    """Apply a raw TT-equivalent delta and return the resulting skill points.

    Supports both positive and negative deltas. The inverse is solved against
    the same cumulative curve used by ``skill_tt_value`` so forward/inverse
    calculations stay consistent.
    """
    current_points = float(current_points)
    delta_tt = float(delta_tt)
    if not (current_points >= 0.0) or current_points == float("inf"):
        raise ValueError("Current skill points must be a non-negative finite number.")
    if current_points > SKILL_TT_CURVE_MAX_POINTS:
        raise ValueError(
            f"Verified skill TT curve currently supports up to {SKILL_TT_CURVE_MAX_POINTS:,} points."
        )
    if delta_tt != delta_tt or abs(delta_tt) == float("inf"):
        raise ValueError("TT delta must be a finite number.")
    if delta_tt == 0.0:
        return current_points

    target_tt = skill_tt_value(current_points) + delta_tt
    min_tt = _SKILL_TT_CURVE[0]
    max_tt = _SKILL_TT_CURVE[-1]
    tolerance = 1e-12
    if target_tt < min_tt - tolerance:
        raise ValueError(
            f"Target TT value {target_tt:.6f} is below the minimum supported value {min_tt:.6f}."
        )
    if target_tt > max_tt + tolerance:
        raise ValueError(
            f"Target TT value {target_tt:.6f} exceeds the verified 20,000-point curve "
            f"({max_tt:.2f} PED)."
        )
    target_tt = min(max(target_tt, min_tt), max_tt)

    lo = 0.0
    hi = float(SKILL_TT_CURVE_MAX_POINTS)
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if skill_tt_value(mid) < target_tt:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0

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


def load_skill_hp_increases() -> dict:
    """Load positive HpIncrease values keyed by exact skill name."""
    result = {}

    skills_data = load_json(SKILLS_DATA_FILE, [])
    if isinstance(skills_data, list):
        for entry in skills_data:
            if not isinstance(entry, dict):
                continue
            skill_name = str(entry.get("Name", "") or "").strip()
            properties = entry.get("Properties", {}) or {}
            hp_increase = parse_float(properties.get("HpIncrease"), 0.0)
            if skill_name and hp_increase > 0:
                result[skill_name] = hp_increase

    professions_data = load_json(PROFESSIONS_DATA_FILE, [])
    if isinstance(professions_data, list):
        for profession in professions_data:
            if not isinstance(profession, dict):
                continue
            for row in profession.get("Skills", []) or []:
                if not isinstance(row, dict):
                    continue
                skill = row.get("Skill", {}) or {}
                skill_name = str(skill.get("Name", "") or "").strip()
                properties = skill.get("Properties", {}) or {}
                hp_increase = parse_float(properties.get("HpIncrease"), 0.0)
                if skill_name and hp_increase > 0 and skill_name not in result:
                    result[skill_name] = hp_increase

    return result


def skill_hp_gain(skill_name: str, point_gain: float) -> float:
    hp_increase = parse_float(SKILL_HP_INCREASES.get(str(skill_name).strip()), 0.0)
    if hp_increase <= 0:
        return 0.0
    return max(0.0, parse_float(point_gain, 0.0)) / hp_increase



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


def _session_skill_start_points(session: dict, skill_name: str):
    """Best-effort starting point for a saved session skill.

    Newer sessions store a full start snapshot. Older sessions may still carry
    per-event before/after points, which are sufficient for migration too.
    """
    start_snapshot = session.get("current_skills_at_start", {}) or {}
    if skill_name in start_snapshot:
        return parse_float(start_snapshot.get(skill_name), None)

    for event in list(session.get("events", []) or []):
        if event.get("type") != "skill_gain" or str(event.get("skill", "")) != str(skill_name):
            continue
        before = parse_float(event.get("skill_points_before"), None)
        if before is not None:
            return before
    return None


def recalculate_saved_session_skill_tt(session: dict) -> bool:
    """Recalculate an old saved session with the verified raw-TT curve.

    The migration is deliberately all-or-nothing per session. If any gained
    skill lacks a reliable starting point or lies outside the verified curve,
    nothing in that session is changed, avoiding a mixture of old and new TT
    models in one row. Returns True when the session was updated.
    """
    if not isinstance(session, dict):
        return False
    if session.get("skill_tt_curve_version") == SKILL_TT_CURVE_VERSION:
        return False

    point_gains = session.get("skill_gains_points", {}) or {}
    if not isinstance(point_gains, dict):
        return False
    if not point_gains:
        # No skill TT values to migrate, but mark the empty session so startup
        # does not revisit it forever.
        session["skill_gains_tt"] = {}
        session["skill_gain_tt_total"] = 0.0
        session["skill_tt_curve_version"] = SKILL_TT_CURVE_VERSION
        return True

    recalculated = {}
    for skill_name, raw_gain in point_gains.items():
        point_gain = parse_float(raw_gain, None)
        start_points = _session_skill_start_points(session, str(skill_name))
        if point_gain is None or start_points is None:
            return False
        end_points = start_points + point_gain
        try:
            tt_gain = skill_tt_value(end_points) - skill_tt_value(start_points)
        except (TypeError, ValueError):
            return False
        recalculated[str(skill_name)] = float(tt_gain)

    # Update per-message TT values too when their before/after points are saved.
    for event in list(session.get("events", []) or []):
        if event.get("type") != "skill_gain":
            continue
        before = parse_float(event.get("skill_points_before"), None)
        after = parse_float(event.get("skill_points_after"), None)
        if before is None or after is None:
            continue
        try:
            event["tt_gain"] = float(skill_tt_value(after) - skill_tt_value(before))
        except (TypeError, ValueError):
            # Session totals remain valid; leave this legacy event detail alone
            # when it cannot be represented by the verified range.
            pass

    session["skill_gains_tt"] = recalculated
    session["skill_gain_tt_total"] = float(sum(recalculated.values()))
    session["skill_gain_mode"] = "chat_points_plus_raw_tt_equivalent"
    session["skill_tt_curve_version"] = SKILL_TT_CURVE_VERSION
    return True


def migrate_saved_session_skill_tt(sessions) -> int:
    changed = 0
    for session in list(sessions or []):
        if recalculate_saved_session_skill_tt(session):
            changed += 1
    return changed


# Initialize HP metadata only after parse_float() is available.
SKILL_HP_INCREASES = load_skill_hp_increases()


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
    skill_gain_mode: str = "chat_points_plus_raw_tt_equivalent"
    skill_tt_curve_version: str = SKILL_TT_CURVE_VERSION
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
    # Keep the pre-edit representation too, including fields from old releases
    # that may not survive legacy loot grouping/quantity normalization.
    loot_events_before_exclusion: list = field(default_factory=list)
    loot_event_grouping_version: int = LOOT_EVENT_GROUPING_VERSION
    loot_events: list = field(default_factory=list)
    ped_cycled: float = 0.0
    notes: str = ""
    events: list = field(default_factory=list)


class ChatLogParser:
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^]]*)\] \[([^]]*)\] (.*)$")
    gain_direct_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) ([A-Za-z][A-Za-z '\-]+)$")
    gain_experience_re = re.compile(r"^You have gained ([0-9]+(?:\.[0-9]+)?) experience in your (.+?) skill$")
    improved_re = re.compile(r"^Your (.+?) has improved by ([0-9]+(?:\.[0-9]+)?)$")
    normal_damage_re = re.compile(
        r"^You inflicted ([0-9]+(?:\.[0-9]+)?) points of damage(?:\. Target resisted some additional damage)?$"
    )
    resisted_all_damage_re = re.compile(r"^The target resisted all damage$")
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

        if cls.resisted_all_damage_re.match(message):
            return {"type": "normal_hit", "timestamp": timestamp, "damage": 0.0, "message": message}

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


class SessionDerivedCache:
    """Disposable, in-memory summaries; never serialize cache fields into sessions.

    Keep small item totals for history/analysis, but bound the much larger
    normalized event lists. All application loot mutations explicitly invalidate
    their entry. Identity/length guards also detect replaced/imported histories.
    """
    def __init__(self, event_limit=6, event_budget=25000):
        self.entries = {}
        self.events = OrderedDict()
        self.event_limit = event_limit
        self.event_budget = event_budget
        self.serial = 0

    @staticmethod
    def signature(session):
        rows = session.get("loot_events") or ()
        raw = session.get("events") or ()
        version = parse_float(session.get("loot_event_grouping_version"), 0)
        legacy = version < LOOT_EVENT_GROUPING_VERSION
        return (id(rows), len(rows), version,
                (id(raw), len(raw), session.get("count_hunting"), session.get("weapon"),
                 session.get("amplifier"), tuple(session.get("attachments") or [])) if legacy else None)

    def entry(self, session):
        key, signature = id(session), self.signature(session)
        entry = self.entries.get(key)
        if entry is None or entry["source"] is not session or entry["signature"] != signature:
            self.events.pop(key, None)
            self.serial += 1
            entry = {"source": session, "signature": signature, "summary": None, "generation": self.serial}
            self.entries[key] = entry
        return entry

    def invalidate(self, session, *, keep_summary=False):
        key = id(session)
        old = self.entries.get(key)
        summary = old["summary"] if keep_summary and old and old["source"] is session else None
        self.events.pop(key, None)
        self.serial += 1
        self.entries[key] = {"source": session, "signature": self.signature(session), "summary": summary,
                             "generation": self.serial}

    def inherit_summary(self, source, target):
        entry = self.entries.get(id(source))
        if entry and entry["source"] is source and entry["signature"] == self.signature(source):
            self.invalidate(target)
            self.entries[id(target)]["summary"] = entry["summary"]

    def normalized(self, session, build):
        self.entry(session)
        key = id(session)
        if key not in self.events:
            self.events[key] = build(session)
            while len(self.events) > 1 and (len(self.events) > self.event_limit
                                          or sum(len(rows) for rows in self.events.values()) > self.event_budget):
                self.events.popitem(last=False)
        self.events.move_to_end(key)
        return self.events[key]

    def prune(self, sources):
        alive = {id(source) for source in sources if isinstance(source, dict)}
        for key in list(self.entries):
            if key not in alive:
                self.entries.pop(key, None)
                self.events.pop(key, None)


class PageControls(ttk.Frame):
    """Navigate all records while keeping only one page in Tk widgets."""
    def __init__(self, parent, command, page_size=200):
        super().__init__(parent)
        self.command, self.page_size, self.page, self.total = command, page_size, 0, 0
        buttons = ttk.Frame(self)
        buttons.pack(side="left")
        self.first = ttk.Button(buttons, text="First", width=5, command=lambda: self.go(0))
        self.previous = ttk.Button(buttons, text="Prev", width=5, command=lambda: self.go(self.page - 1))
        self.next = ttk.Button(buttons, text="Next", width=5, command=lambda: self.go(self.page + 1))
        self.last = ttk.Button(buttons, text="Last", width=5, command=lambda: self.go(self.pages - 1))
        for button in (self.first, self.previous, self.next, self.last):
            button.pack(side="left", padx=1)
        status = ttk.Frame(self)
        status.pack(side="left")
        self.label = ttk.Label(status)
        self.label.pack(side="left", padx=6)
        self.number = tk.StringVar(self, value="1")
        entry = ttk.Entry(status, textvariable=self.number, width=5)
        entry.pack(side="left")
        entry.bind("<Return>", self.jump)
        ttk.Button(status, text="Go", width=3, command=self.jump).pack(side="left", padx=2)
        self.update_count(0)

    @property
    def pages(self):
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    def update_count(self, total, *, reset=False):
        self.total = total
        self.page = 0 if reset else min(self.page, self.pages - 1)
        start = self.page * self.page_size
        self.label.configure(text=f"{start + 1 if total else 0}-{min(start + self.page_size, total)} / {total}")
        self.number.set(str(self.page + 1))
        for button in (self.first, self.previous):
            button.configure(state="normal" if self.page else "disabled")
        for button in (self.next, self.last):
            button.configure(state="normal" if self.page + 1 < self.pages else "disabled")

    def go(self, page):
        page = max(0, min(page, self.pages - 1))
        changed = page != self.page
        if changed:
            self.page = page
        self.update_count(self.total)
        if changed:
            self.command()

    def jump(self, event=None):
        try:
            self.go(int(self.number.get()) - 1)
        except ValueError:
            self.number.set(str(self.page + 1))


class PagedTree:
    def __init__(self, app, tree, parent, *, page_size=200):
        self.app, self.tree = app, tree
        self.rows, self.order, self.format_row = [], [], None
        self.on_render = None
        self.display_records = {}
        self.controls = PageControls(parent, self.render, page_size)
        app.paged_trees[tree] = self
        tree.bind("<Destroy>", self.destroy, add="+")

    def destroy(self, event):
        if event.widget is self.tree:
            self.app.paged_trees.pop(self.tree, None)
            self.app.tree_sort_state.pop(self.tree, None)
            self.app.tree_heading_titles.pop(self.tree, None)

    def set_rows(self, rows, format_row, *, reset=False, on_render=None):
        self.rows, self.format_row, self.on_render = rows, format_row, on_render
        self.order = list(range(len(rows)))
        self.controls.update_count(len(rows), reset=reset)
        self.sort()

    def sort(self):
        state = self.app.tree_sort_state.get(self.tree, {})
        column = state.get("column")
        if column:
            position = list(self.tree["columns"]).index(column)
            keyed, empty = [], []
            for index in self.order:
                key = self.app.tree_sort_key(self.format_row(self.rows[index])[position])
                (empty if key is None else keyed).append(index if key is None else (key, index))
            keyed.sort(key=lambda pair: pair[0], reverse=bool(state.get("descending")))
            self.order = [index for _, index in keyed] + empty
        self.render()

    def render(self):
        selection = set(self.tree.selection())
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        start = self.controls.page * self.controls.page_size
        visible = [self.rows[index] for index in self.order[start:start + self.controls.page_size]]
        for row in visible:
            iid, _record = row
            self.tree.insert("", "end", iid=iid, values=self.format_row(row))
        kept = [iid for iid, record in visible if iid in selection and self.display_records.get(iid) is record]
        if kept:
            self.tree.selection_set(kept)
        if self.tree.exists(focus) and self.display_records.get(focus) is dict(visible).get(focus):
            self.tree.focus(focus)
        self.display_records = dict(visible)
        if self.on_render:
            self.on_render(visible)


class SkillTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Entropia Skill Tracker")
        self.root.geometry("1280x820")
        self.session_cache = SessionDerivedCache()
        self.paged_trees = {}
        self.loot_view_signature = None
        self.loot_chart_signature = None
        self.loot_base_chart_signature = None
        self.loot_plot_data = None
        self.loot_rows_signature = None
        self.detail_session = None

        self.current_skills = load_current_skills()
        self.state = load_json(TRACKER_STATE_FILE, {})
        self.sessions = load_json(SESSIONS_FILE, [])
        if not isinstance(self.sessions, list):
            self.sessions = []
        self.analysis_sessions = load_json(ANALYSIS_SESSIONS_FILE, [])
        if not isinstance(self.analysis_sessions, list):
            self.analysis_sessions = []

        # Historical sessions contain TT-equivalent totals calculated with the
        # old post-extraction/approximate curve. Rebuild them from saved raw
        # point gains when the stored start snapshot makes that exact.
        migrated_sessions = migrate_saved_session_skill_tt(self.sessions)
        migrated_analysis_sessions = migrate_saved_session_skill_tt(self.analysis_sessions)
        synchronized_analysis = self.synchronized_analysis_sessions(self.sessions)
        if synchronized_analysis != self.analysis_sessions:
            self.analysis_sessions = synchronized_analysis
            migrated_analysis_sessions = True
        if migrated_sessions:
            save_json(SESSIONS_FILE, self.sessions)
        if migrated_analysis_sessions:
            save_json(ANALYSIS_SESSIONS_FILE, self.analysis_sessions)

        self.hunting_setups = load_json(HUNTING_SETUPS_FILE, {})
        if not isinstance(self.hunting_setups, dict):
            self.hunting_setups = {}
        self.favorite_mobs = load_json(FAVORITE_MOBS_FILE, [])
        if not isinstance(self.favorite_mobs, list):
            self.favorite_mobs = []
        # Import old setup targets once, without rewriting their source file.
        # A marker prevents removed favorites from reappearing on restart.
        if not self.state.get("hunting_targets_imported"):
            for setup in self.hunting_setups.values():
                if not isinstance(setup, dict):
                    continue
                mob = str(setup.get("mob", "") or "").strip()
                maturity = str(setup.get("maturity", "") or "").strip()
                if mob and not any(isinstance(row, dict) and row.get("mob") == mob and row.get("maturity", "") == maturity for row in self.favorite_mobs):
                    self.favorite_mobs.append({"mob": mob, "maturity": maturity})
            # Save favorites before the marker so an interrupted import can
            # safely retry without losing the old targets or adding duplicates.
            save_json(FAVORITE_MOBS_FILE, self.favorite_mobs)
            self.state["hunting_targets_imported"] = True
            save_json(TRACKER_STATE_FILE, self.state)
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

        # Live tail polling is intentionally throttled. Older versions checked
        # chat.log every 100 ms and called save_state() even when there were no
        # new lines. save_state() fingerprints the log and fsyncs JSON files, so
        # during hunting that could repeatedly block Tkinter and make sync look
        # frozen.
        self.log_poll_interval = 0.25
        self.next_log_poll_at = 0.0
        self.log_fingerprint_check_interval = 3.0
        self.last_log_fingerprint_check_at = 0.0
        self.monitor_tick_errors = 0

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
        saved_projection_profession = self.state.get("projection_profession", default_projection_profession)
        if saved_projection_profession not in PROFESSIONS:
            saved_projection_profession = default_projection_profession
        self.session_projection_profession_var = tk.StringVar(value=saved_projection_profession)
        self.session_projection_ped_var = tk.StringVar(value=str(self.state.get("projection_ped_cycle", "1000")))
        self.projection_refresh_after_id = None
        self.total_gain_var = tk.StringVar(value="Total profession gain: 0.0000")
        self.profession_skill_drafts = {}
        self.profession_cell_editor = None
        self.profession_cell_editor_meta = None
        self.profession_cell_committing = False
        self.entries = {}

        self.chat_log_path_var = tk.StringVar(value=self.last_log_path)
        self.monitor_status_var = tk.StringVar(value="Stopped")
        self.monitor_progress_var = tk.StringVar(value="")
        self.session_summary_var = tk.StringVar(value="No active session")
        self.monitor_metric_vars = {
            key: tk.StringVar(value="—")
            for key in ("ped", "loot", "damage", "kills", "cost", "dpp", "effective_dpp", "skill_tt")
        }
        self.monitor_combat_var = tk.StringVar(value="")
        self.monitor_skills_var = tk.StringVar(value="")
        self.monitor_projection_var = tk.StringVar(value="Start a session to project profession and HP gains.")
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
        self.hunting_setup_status_var = tk.StringVar(value="Name and save current equipment to add a setup.")
        self.favorite_mob_status_var = tk.StringVar(value="Choose a target below to add it to favorites.")
        saved_style, saved_scheme = saved_ui_appearance(self.state)
        self.ui_style_var = tk.StringVar(value=saved_style)
        self.ui_color_scheme_var = tk.StringVar(value=saved_scheme)
        # Retain the old internal layout variable for existing integrations.
        self.ui_theme_var = self.ui_style_var
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
        self.session_cell_editor = None
        self.session_cell_editor_meta = None
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
        for projection_var in (self.session_projection_profession_var, self.session_projection_ped_var):
            projection_var.trace_add("write", self.schedule_projection_refresh)
        self.load_profession()
        self.refresh_hunting_info()
        self.refresh_sessions_table()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(500, self.monitor_tick)

    def create_ui(self):
        self.configure_ui_styles()
        header = ttk.Frame(self.root, padding=(12, 8))
        header.pack(fill="x")
        self.theme_combo = ttk.Combobox(header, textvariable=self.ui_color_scheme_var, values=list(UI_COLOR_SCHEMES), state="readonly", width=8)
        self.theme_combo.pack(side="right")
        self.theme_combo.bind("<<ComboboxSelected>>", self.apply_ui_theme)
        ttk.Label(header, text="Theme:").pack(side="right", padx=8)
        self.style_combo = ttk.Combobox(header, textvariable=self.ui_style_var, values=UI_STYLES, state="readonly", width=13)
        self.style_combo.pack(side="right")
        self.style_combo.bind("<<ComboboxSelected>>", self.apply_ui_theme)
        ttk.Label(header, text="Style:").pack(side="right", padx=8)

        self.ui_body = ttk.Frame(self.root)
        self.ui_body.pack(fill="both", expand=True)
        self.ui_body.rowconfigure(0, weight=1)
        self.ui_body.columnconfigure(1, weight=1)
        self.sidebar = ttk.Frame(self.ui_body, padding=(10, 12))
        self.sidebar.grid(row=0, column=0, sticky="ns")
        notebook = ttk.Notebook(self.ui_body)
        self.notebook = notebook
        notebook.grid(row=0, column=1, sticky="nsew")

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
        notebook.add(self.session_details_tab, text="Session Details")
        notebook.add(self.mob_analysis_tab, text="What Mob to Hunt")
        notebook.add(self.profession_tab, text="Professions / Skills")

        self.navigation_buttons = {}
        for tab in notebook.tabs():
            button = ttk.Button(self.sidebar, text=notebook.tab(tab, "text"), width=22,
                                style="Tracker.Nav.TButton", command=lambda tab=tab: notebook.select(tab))
            button.pack(fill="x", pady=3)
            self.navigation_buttons[tab] = button

        self.create_monitor_tab()
        self.create_loot_tab()
        self.create_hunting_tab()
        self.create_sessions_tab()
        self.create_mob_analysis_tab()
        self.create_session_details_tab()
        self.create_profession_tab()
        self.style_tracker_widgets(self.root)
        self.configure_ui_navigation()
        notebook.bind("<<NotebookTabChanged>>", self.on_notebook_tab_changed)

    def is_tab_active(self, tab):
        try:
            return self.notebook.select() == str(tab)
        except (AttributeError, tk.TclError):
            return False

    def on_notebook_tab_changed(self, event=None):
        self.update_navigation_selection()
        # Heavy loot tables/charts are refreshed on demand when their tab is
        # opened, rather than after every parsed log batch.
        if self.is_tab_active(getattr(self, "loot_tab", None)):
            self.refresh_loot_tab(force=False)
            self.last_loot_live_refresh_at = time.monotonic()
        elif self.is_tab_active(getattr(self, "profession_tab", None)):
            self.refresh_profession_current_values()
        elif self.is_tab_active(getattr(self, "mob_analysis_tab", None)):
            self.refresh_mob_analysis(persist_settings=False)
        elif self.is_tab_active(getattr(self, "session_details_tab", None)):
            self.show_session_details(self.selected_session_from_table())

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
        pager = self.paged_trees.get(tree)
        if pager is not None:
            pager.controls.update_count(len(pager.rows), reset=True)
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
        pager = getattr(self, "paged_trees", {}).get(tree)
        if pager is not None:
            pager.sort()
            return
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
        tree.set_children("", *ordered_items)

    def create_profession_tab(self):
        top_frame = ttk.LabelFrame(self.profession_tab, text="Profession", padding=10)
        top_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(top_frame, text="Profession:").grid(row=0, column=0, sticky="w")
        self.profession_combo = ttk.Combobox(
            top_frame,
            textvariable=self.profession_var,
            values=list(PROFESSIONS.keys()),
            width=45,
            state="readonly",
        )
        self.profession_combo.grid(row=0, column=1, sticky="ew", padx=8)
        self.profession_var.set("Animal Looter" if "Animal Looter" in PROFESSIONS else list(PROFESSIONS.keys())[0])
        self.profession_combo.bind("<<ComboboxSelected>>", lambda e: self.load_profession())

        ttk.Label(top_frame, textvariable=self.total_gain_var, style="Tracker.Emphasis.TLabel").grid(row=0, column=2, sticky="e", padx=10)
        actions = ttk.Frame(top_frame)
        actions.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Save New Values", command=self.save_current_skills_from_table).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="Reload Saved Skills", command=self.reload_saved_skills).pack(side="left", padx=5)
        ttk.Label(top_frame, text="Double-click a value to edit; Enter or leaving the cell recalculates, Esc cancels. Edits are saved only with Save New Values.", wraplength=600).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(top_frame, text="100 profession gain = 1 profession level.").grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        top_frame.columnconfigure(1, weight=1)

        columns = ("skill", "weight", "current", "delta", "new", "skill_gain", "profession_gain")
        self.skill_tree = ttk.Treeview(self.profession_tab, columns=columns, show="headings", height=24)
        headings = {
            "skill": "Skill", "weight": "Weight %", "current": "Current value", "delta": "TT delta",
            "new": "New value", "skill_gain": "Skill gain", "profession_gain": "Profession gain"
        }
        widths = {"skill": 250, "weight": 90, "current": 130, "delta": 120, "new": 130, "skill_gain": 130, "profession_gain": 150}
        for col in columns:
            self.skill_tree.heading(col, text=headings[col])
            self.skill_tree.column(col, width=widths[col], anchor="center" if col != "skill" else "w")
        table_holder = self.pack_table(self.skill_tree, self.profession_tab, padx=10, pady=(0, 10))
        for scrollbar in table_holder.children.values():
            view = self.skill_tree.yview if scrollbar.cget("orient") == "vertical" else self.skill_tree.xview
            scrollbar.configure(command=lambda *args, view=view: self.scroll_profession_table(view, *args))
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.skill_tree.bind(event, lambda event: None if self.commit_profession_cell_edit() else "break", add="+")
        self.skill_tree.bind("<Configure>", self.position_profession_cell_editor)
        self.skill_tree.bind("<Double-1>", self.on_profession_skill_double_click)

    def create_monitor_tab(self):
        top = ttk.Frame(self.monitor_tab, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="chat.log:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.chat_log_path_var).grid(row=0, column=1, columnspan=5, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self.browse_chat_log).grid(row=0, column=6, padx=4)
        actions = ttk.Frame(top)
        actions.grid(row=1, column=0, columnspan=7, sticky="ew", pady=8)
        ttk.Button(actions, text="Start Sync", command=self.start_sync).pack(side="left")
        ttk.Button(actions, text="Stop Sync", command=self.stop_sync).pack(side="left", padx=6)
        self.pause_sync_button = ttk.Button(actions, text="Pause Sync", command=self.toggle_pause_sync)
        self.pause_sync_button.pack(side="left")
        ttk.Label(actions, textvariable=self.monitor_status_var).pack(side="right", padx=8)
        ttk.Label(top, text="Start from:").grid(row=2, column=0, sticky="w")
        ttk.Combobox(
            top,
            textvariable=self.sync_start_mode_var,
            values=self.sync_start_modes,
            width=24,
            state="readonly",
        ).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(top, text="Last read:").grid(row=2, column=2, sticky="e", padx=(8, 4))
        ttk.Entry(top, textvariable=self.last_log_read_at_var, width=25).grid(row=2, column=3, sticky="w")
        ttk.Button(top, text="Save Time", command=self.save_last_log_read_at_from_ui).grid(row=2, column=4, sticky="w", padx=4)
        ttk.Button(top, text="Clear Time", command=self.clear_last_log_read_at).grid(row=2, column=5, sticky="w", padx=4)
        ttk.Button(top, text="Reload Time", command=self.reload_last_log_read_at).grid(row=2, column=6, sticky="w", padx=4)
        ttk.Label(top, textvariable=self.monitor_progress_var).grid(row=3, column=0, columnspan=7, sticky="w", pady=(4, 0))
        top.columnconfigure(1, weight=1)

        summary = ttk.LabelFrame(self.monitor_tab, text="Current session", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        self.add_wrapped_label(summary, self.session_summary_var)
        metrics = ttk.Frame(summary)
        metrics.pack(fill="x", pady=(8, 6))
        for index, (key, title) in enumerate((
            ("ped", "PED cycled"), ("loot", "Loot TT / Return"),
            ("damage", "Total damage"), ("kills", "Loot events / Kills"),
            ("cost", "Average cost / Kill (PED)"), ("dpp", "DPP (damage / PEC)"),
            ("effective_dpp", "Effective DPP (damage / PEC)"), ("skill_tt", "Skill TT / Cycled"),
        )):
            row, column = divmod(index, 4)
            card = ttk.Frame(metrics, padding=(10, 6))
            card.grid(row=row, column=column, sticky="nsew")
            metrics.columnconfigure(column, weight=1, uniform="monitor_metrics")
            ttk.Label(card, text=title, style="Tracker.Caption.TLabel", wraplength=220).pack(anchor="w")
            ttk.Label(card, textvariable=self.monitor_metric_vars[key], style="Tracker.Value.TLabel").pack(anchor="w", pady=(3, 0))
        self.add_wrapped_label(summary, self.monitor_combat_var)
        self.add_wrapped_label(summary, self.monitor_skills_var)

        projection = ttk.LabelFrame(self.monitor_tab, text="Profession projection", padding=10)
        projection.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(projection, text="Profession:").grid(row=0, column=0, sticky="w")
        self.monitor_projection_combo = ttk.Combobox(
            projection, textvariable=self.session_projection_profession_var,
            values=sorted(PROFESSIONS.keys(), key=str.lower), state="readonly", width=35,
        )
        self.monitor_projection_combo.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(projection, text="PED to cycle:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.monitor_projection_ped_entry = ttk.Entry(projection, textvariable=self.session_projection_ped_var, width=14)
        self.monitor_projection_ped_entry.grid(row=0, column=3, sticky="w", padx=8)
        self.monitor_projection_ped_entry.bind("<Return>", self.refresh_projection_views)
        ttk.Button(projection, text="Calculate", command=self.refresh_projection_views).grid(row=0, column=4, padx=4)
        projection.columnconfigure(1, weight=1)
        projection_result = ttk.Frame(projection)
        projection_result.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.add_wrapped_label(projection_result, self.monitor_projection_var)

        body = ttk.Panedwindow(self.monitor_tab, orient="vertical")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        skill_frame = ttk.LabelFrame(body, text="Session skill gains", padding=6)
        body.add(skill_frame, weight=3)

        columns = ("skill", "tt_gain", "tt_percent", "point_gain", "gain_count", "message_percent", "current")
        self.session_skill_tree = ttk.Treeview(skill_frame, columns=columns, show="headings", height=7)
        for col, title, width in [
            ("skill", "Skill", 280), ("tt_gain", "TT-equivalent gain", 160),
            ("tt_percent", "TT % of skills", 120),
            ("point_gain", "Session point gain", 170), ("gain_count", "Gain messages", 130),
            ("message_percent", "Msg % of skills", 120),
            ("current", "Current skill", 160),
        ]:
            self.session_skill_tree.heading(col, text=title)
            self.session_skill_tree.column(col, width=width, minwidth=width, anchor="center" if col != "skill" else "w")
        self.make_tree_sortable(self.session_skill_tree, {
            "skill": "Skill",
            "tt_gain": "TT-equivalent gain",
            "tt_percent": "TT % of skills",
            "point_gain": "Session point gain",
            "gain_count": "Gain messages",
            "message_percent": "Msg % of skills",
            "current": "Current skill",
        })
        skill_y = ttk.Scrollbar(skill_frame, orient="vertical", command=self.session_skill_tree.yview)
        skill_x = ttk.Scrollbar(skill_frame, orient="horizontal", command=self.session_skill_tree.xview)
        self.session_skill_tree.configure(yscrollcommand=skill_y.set, xscrollcommand=skill_x.set)
        self.session_skill_tree.grid(row=0, column=0, sticky="nsew")
        skill_y.grid(row=0, column=1, sticky="ns")
        skill_x.grid(row=1, column=0, sticky="ew")
        skill_frame.columnconfigure(0, weight=1)
        skill_frame.rowconfigure(0, weight=1)
        self.session_skill_tree.bind(
            "<Double-1>",
            lambda event: self.open_skill_gain_details_from_tree(event, self.session_skill_tree, "active"),
        )

        event_frame = ttk.LabelFrame(body, text="Recent parsed events", padding=6)
        body.add(event_frame, weight=1)
        self.event_text = tk.Text(event_frame, height=4, wrap="none")
        event_y = ttk.Scrollbar(event_frame, orient="vertical", command=self.event_text.yview)
        event_x = ttk.Scrollbar(event_frame, orient="horizontal", command=self.event_text.xview)
        self.event_text.configure(yscrollcommand=event_y.set, xscrollcommand=event_x.set)
        self.event_text.grid(row=0, column=0, sticky="nsew")
        event_y.grid(row=0, column=1, sticky="ns")
        event_x.grid(row=1, column=0, sticky="ew")
        event_frame.columnconfigure(0, weight=1)
        event_frame.rowconfigure(0, weight=1)
    def configure_ui_styles(self):
        """Update existing widgets through shared styles; never rebuild a tab."""
        style = ttk.Style(self.root)
        if style.theme_use() != "clam":
            style.theme_use("clam")
        ui_style = self.ui_style_var.get()
        self.ui_colors = UI_COLOR_SCHEMES.get(self.ui_color_scheme_var.get(), UI_COLOR_SCHEMES["Light"])
        colors = self.ui_colors
        background, surface, ink, accent, border = (colors[key] for key in ("background", "surface", "ink", "accent", "border"))
        self.ui_font_family = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
        font = self.ui_font_family
        self.ui_font = (font, 9)
        self.ui_log_font = ("Consolas" if os.name == "nt" else "DejaVu Sans Mono", 9)
        self.root.configure(background=background)
        self.root.option_add("*Font", self.ui_font)
        self.root.option_add("*TCombobox*Listbox.font", self.ui_font)
        for option, value in (("background", colors["field"]), ("foreground", ink),
                              ("selectBackground", colors["selection"]), ("selectForeground", colors["selection_ink"])):
            self.root.option_add(f"*TCombobox*Listbox.{option}", value)
        style.configure(".", font=self.ui_font, background=background, foreground=ink, bordercolor=border)
        style.configure("TFrame", background=background)
        style.configure("Tracker.Surface.TFrame", background=surface)
        style.configure("TLabelframe", background=surface, bordercolor=border, borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=surface, foreground=ink, font=(font, 10, "bold"))
        style.configure("TLabel", background=background, foreground=ink, font=self.ui_font)
        style.configure("Tracker.Surface.TLabel", background=surface, foreground=ink, font=self.ui_font)
        style.configure("Tracker.Brand.TLabel", background=background, foreground=ink, font=(font, 12, "bold"))
        style.configure("Tracker.Caption.TLabel", background=surface, foreground=colors["muted"], font=(font, 8))
        style.configure("Tracker.Value.TLabel", background=surface, foreground=accent, font=(font, 16 if ui_style == "Command" else 14, "bold"))
        style.configure("Tracker.Emphasis.TLabel", background=surface, foreground=accent, font=(font, 11, "bold"))
        style.configure("TButton", font=self.ui_font, padding=(8, 4), background=surface, foreground=ink,
                        bordercolor=border, lightcolor=border, darkcolor=border, focusthickness=1, focuscolor=accent)
        style.map("TButton", background=[("disabled", background), ("pressed", colors["pressed"]), ("active", colors["hover"])],
                  foreground=[("disabled", colors["disabled"])])
        for selected, widget_style in ((False, "Tracker.Nav.TButton"), (True, "Tracker.Selected.Nav.TButton")):
            nav_background = colors["pressed"] if selected else background
            style.configure(widget_style, padding=(10, 9), anchor="w", background=nav_background,
                            foreground=accent if selected else ink, bordercolor=nav_background,
                            lightcolor=nav_background, darkcolor=nav_background)
            style.map(widget_style, background=[("pressed", colors["pressed"]), ("active", colors["hover"])])
        for widget_style in ("TEntry", "TCombobox"):
            style.configure(widget_style, font=self.ui_font, padding=4, fieldbackground=colors["field"], foreground=ink,
                            insertcolor=ink, arrowcolor=colors["muted"], bordercolor=border, lightcolor=border, darkcolor=border,
                            selectbackground=colors["selection"], selectforeground=colors["selection_ink"])
            style.map(widget_style, fieldbackground=[("disabled", background), ("readonly", colors["field"])],
                      foreground=[("disabled", colors["disabled"]), ("readonly", ink)], bordercolor=[("focus", accent)])
        for widget_style in ("TCheckbutton", "Tracker.Surface.TCheckbutton"):
            widget_background = surface if "Surface" in widget_style else background
            style.configure(widget_style, background=widget_background, foreground=ink, font=self.ui_font,
                            indicatorbackground=colors["field"], indicatorforeground=accent,
                            bordercolor=border, lightcolor=border, darkcolor=border)
            style.map(widget_style, background=[("active", widget_background)], foreground=[("disabled", colors["disabled"])],
                      indicatorbackground=[("selected", colors["pressed"]), ("active", colors["hover"])])
        style.configure("Treeview", font=self.ui_font, rowheight=26, background=surface, fieldbackground=surface,
                        foreground=ink, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("Treeview.Heading", font=(font, 9, "bold"), padding=(6, 5), background=colors["heading"],
                        foreground=ink, bordercolor=border, lightcolor=border, darkcolor=border)
        style.map("Treeview.Heading", background=[("active", colors["hover"])])
        style.map("Treeview", background=[("selected", colors["selection"])], foreground=[("selected", colors["selection_ink"])])
        style.configure("TNotebook", background=background, borderwidth=0, bordercolor=border)
        style.configure("TNotebook.Tab", font=self.ui_font, padding=(12, 7), background=background, foreground=colors["muted"],
                        bordercolor=border, lightcolor=border, darkcolor=border)
        style.map("TNotebook.Tab", background=[("selected", surface), ("active", colors["hover"])], foreground=[("selected", accent)])
        style.layout("Tracker.Sidebar.TNotebook.Tab", [])
        style.configure("Tracker.Sidebar.TNotebook", background=background, borderwidth=0, tabmargins=0)
        for widget_style in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(widget_style, background=colors["scrollbar"], troughcolor=background,
                            bordercolor=background, lightcolor=border, darkcolor=border, arrowcolor=colors["muted"])
            style.map(widget_style, background=[("active", colors["hover"]), ("pressed", colors["pressed"])])
        style.configure("TPanedwindow", background=background)
        style.configure("Sash", background=border)

    def configure_ui_navigation(self):
        if self.ui_style_var.get() == "Command":
            self.sidebar.grid()
            self.notebook.configure(style="Tracker.Sidebar.TNotebook")
        else:
            self.sidebar.grid_remove()
            self.notebook.configure(style="TNotebook")
        self.update_navigation_selection()

    def update_navigation_selection(self):
        selected = self.notebook.select()
        for tab, button in self.navigation_buttons.items():
            button.configure(style="Tracker.Selected.Nav.TButton" if tab == selected else "Tracker.Nav.TButton")

    def apply_ui_theme(self, event=None):
        if self.ui_style_var.get() not in UI_STYLES:
            self.ui_style_var.set("Compact")
        if self.ui_color_scheme_var.get() not in UI_COLOR_SCHEMES:
            self.ui_color_scheme_var.set("Light")
        self.configure_ui_styles()
        self.configure_ui_navigation()
        self.style_tracker_widgets(self.root)
        self.sessions_tree.tag_configure("analysis_valid", background=self.ui_colors["valid_session"])
        # Recolor cached chart data rather than resetting filters or zoom.
        self.root.after_idle(self.redraw_theme_charts)
        # Persist only appearance. Saving equipment here would normalize
        # unfinished combobox input and could discard the user's edits.
        self.state.update(ui_style=self.ui_style_var.get(), ui_color_scheme=self.ui_color_scheme_var.get(),
                          ui_theme=self.ui_style_var.get())
        save_json(TRACKER_STATE_FILE, self.state)

    def redraw_theme_charts(self):
        for canvas in (self.loot_value_time_canvas, self.loot_cost_canvas, self.loot_items_time_canvas):
            if canvas.winfo_exists():
                self.redraw_chart_canvas(canvas)

    def style_tracker_widgets(self, parent, surface=False):
        widget = parent
        widget_class = widget.winfo_class()
        on_surface = surface or widget_class == "TLabelframe"
        styles = {
            "TFrame": "Tracker.Surface.TFrame" if on_surface else "TFrame",
            "TLabel": "Tracker.Surface.TLabel" if on_surface else "TLabel",
            "TCheckbutton": "Tracker.Surface.TCheckbutton" if on_surface else "TCheckbutton",
        }
        if widget_class in styles and not widget.cget("style"):
            widget.configure(style=styles[widget_class])
        if widget_class in ("Tk", "Toplevel"):
            widget.configure(background=self.ui_colors["background"])
        elif widget_class == "Text":
            widget.configure(font=self.ui_log_font, background=self.ui_colors["surface"], foreground=self.ui_colors["ink"],
                             insertbackground=self.ui_colors["ink"], selectbackground=self.ui_colors["selection"],
                             selectforeground=self.ui_colors["selection_ink"], highlightbackground=self.ui_colors["border"],
                             highlightcolor=self.ui_colors["accent"], highlightthickness=1, borderwidth=0, padx=6, pady=4)
        elif widget_class == "Canvas":
            widget.configure(background=self.ui_colors["surface" if on_surface else "background"],
                             highlightbackground=self.ui_colors["border"], highlightcolor=self.ui_colors["accent"])
        elif widget_class == "Treeview":
            if not getattr(widget, "tracker_columns_styled", False):
                for column in widget["columns"]:
                    widget.column(column, minwidth=int(widget.column(column, "width")))
                widget.tracker_columns_styled = True
        elif widget_class == "TCombobox":
            # Popdowns created by Tcl aren't Python child widgets. Recolor an
            # existing listbox too; the option database covers future popdowns.
            listbox = f"{widget}.popdown.f.l"
            if self.root.tk.call("winfo", "exists", listbox):
                self.root.tk.call(listbox, "configure", "-background", self.ui_colors["field"],
                                  "-foreground", self.ui_colors["ink"], "-selectbackground", self.ui_colors["selection"],
                                  "-selectforeground", self.ui_colors["selection_ink"])
        elif widget_class == "TLabel" and widget.winfo_manager() == "pack":
            if widget.pack_info().get("side") == "top" and not widget.bind("<Configure>"):
                widget.pack_configure(fill="x")
                widget.configure(wraplength=600)
                widget.bind("<Configure>", lambda event, label=widget: label.configure(wraplength=max(1, event.width)))
        for child in list(widget.children.values()):
            self.style_tracker_widgets(child, on_surface)

    def pack_table(self, tree, parent, *, padx=0, pady=0):
        """Mount a table with the same scrolling and spacing in every tab."""
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True, padx=padx, pady=pady)
        tree.grid(in_=holder, row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        # The table retains its original parent for existing callbacks. Keep it
        # above the sibling holder when grid manages it inside that holder.
        tree.lift()
        return holder

    def scrollable_tab_content(self, tab):
        canvas = tk.Canvas(tab, highlightthickness=0)
        canvas.tracker_scroll_container = True
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        if not getattr(self, "tab_scroll_bindings_installed", False):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.bind(sequence, self.scroll_tab_content, add="+")
            self.tab_scroll_bindings_installed = True
        return content

    def scroll_tab_content(self, event):
        widget = event.widget
        # Tables, logs and dropdowns keep their own wheel behavior.
        if widget.winfo_class() in ("Treeview", "Text", "TCombobox"):
            return
        while widget is not None:
            if getattr(widget, "tracker_scroll_container", False):
                if widget.yview() != (0.0, 1.0):
                    direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
                    widget.yview_scroll(direction * 3, "units")
                    return "break"
                return
            widget = widget.master

    def add_wrapped_label(self, parent, variable):
        label = ttk.Label(parent, textvariable=variable, justify="left", anchor="w")
        label.pack(fill="x", anchor="w")
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width)))
        return label

    def schedule_projection_refresh(self, *_args):
        if self.projection_refresh_after_id is not None:
            self.root.after_cancel(self.projection_refresh_after_id)
        self.projection_refresh_after_id = self.root.after(200, self.refresh_projection_views)

    def refresh_projection_views(self, event=None):
        if self.projection_refresh_after_id is not None:
            self.root.after_cancel(self.projection_refresh_after_id)
            self.projection_refresh_after_id = None
        self.refresh_selected_session_details()


    def create_loot_tab(self):
        top = ttk.Frame(self.loot_tab, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh", command=lambda: self.refresh_loot_tab(force=True)).pack(side="right", padx=4)
        ttk.Button(top, text="Reset zoom", command=self.reset_loot_zoom).pack(side="right", padx=4)
        self.restore_loot_button = ttk.Button(top, text="Restore excluded loot", command=self.restore_excluded_loot)
        self.restore_loot_button.pack(side="right", padx=4)
        source_hint = ttk.Label(top, text="Active or selected saved session. Double-click an item/event to exclude individual drops.", justify="left")
        source_hint.pack(side="left", fill="x", expand=True)
        source_hint.bind("<Configure>", lambda event: source_hint.configure(wraplength=max(1, event.width)))

        summary = ttk.LabelFrame(self.loot_tab, text="Loot summary", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        self.add_wrapped_label(summary, self.loot_summary_var)

        item_summary_frame = ttk.LabelFrame(self.loot_tab, text="Looted items grouped by name", padding=6)
        item_summary_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.add_wrapped_label(item_summary_frame, self.loot_markup_status_var)
        item_columns = ("item", "quantity", "value", "loot_percent", "markup", "after_mu")
        self.loot_item_summary_tree = ttk.Treeview(
            item_summary_frame,
            columns=item_columns,
            show="headings",
            height=3,
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
        self.loot_tab.bind("<Configure>", self.resize_loot_layout, add="+")

        selection = ttk.LabelFrame(self.loot_tab, text="Graph selection / zoom", padding=8)
        selection.pack(fill="x", padx=10, pady=(0, 6))
        self.add_wrapped_label(selection, self.loot_selection_var)

        body = ttk.Panedwindow(self.loot_tab, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=6)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=4)

        # Keep all three charts readable rather than clipping the last chart
        # when the summary and item table leave little vertical space.
        self.loot_graphs_canvas = tk.Canvas(right, highlightthickness=0)
        graphs_scroll = ttk.Scrollbar(right, orient="vertical", command=self.loot_graphs_canvas.yview)
        self.loot_graphs_canvas.configure(yscrollcommand=graphs_scroll.set)
        graphs_scroll.pack(side="right", fill="y")
        self.loot_graphs_canvas.pack(side="left", fill="both", expand=True)
        graphs = ttk.Frame(self.loot_graphs_canvas)
        graphs_window = self.loot_graphs_canvas.create_window((0, 0), window=graphs, anchor="nw")
        graphs.bind("<Configure>", lambda event: self.loot_graphs_canvas.configure(scrollregion=self.loot_graphs_canvas.bbox("all")))
        self.loot_graphs_canvas.bind("<Configure>", lambda event: self.loot_graphs_canvas.itemconfigure(graphs_window, width=event.width))

        left_panels = ttk.Panedwindow(left, orient="vertical")
        left_panels.pack(fill="both", expand=True)
        def balance_left_panels(event):
            if event.height <= 50:
                return
            previous_height = getattr(left_panels, "previous_height", None)
            fraction = left_panels.sashpos(0) / previous_height if previous_height else 0.4
            # Keep a usable checkbox viewport and at least one receipt row at
            # smaller window sizes; paging controls must not squeeze either
            # pane down to just its headings.
            maximum = max(0, event.height - 185)
            minimum = min(140, maximum)
            left_panels.sashpos(0, max(minimum, min(maximum, int(event.height * fraction))))
            left_panels.previous_height = event.height
        left_panels.bind("<Configure>", balance_left_panels)
        items_frame = ttk.LabelFrame(left_panels, text="Item filter", padding=6)
        left_panels.add(items_frame, weight=1)
        ttk.Label(items_frame, text="Filter item names:").pack(anchor="w")
        item_filter = ttk.Entry(items_frame, textvariable=self.loot_item_filter_var)
        item_filter.pack(fill="x", pady=(2, 6))
        item_filter.bind("<KeyRelease>", lambda event: self.refresh_loot_item_checks(force=True))
        filter_actions = ttk.Frame(items_frame)
        filter_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(filter_actions, text="Apply selected items", command=lambda: self.refresh_loot_tab(force=False)).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(filter_actions, text="Clear selection", command=self.clear_loot_item_selection).pack(side="left", fill="x", expand=True)

        canvas_holder = ttk.Frame(items_frame)
        canvas_holder.pack(fill="both", expand=True)
        self.loot_items_canvas = tk.Canvas(canvas_holder, highlightthickness=0, width=260, height=60)
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

        events_frame = ttk.LabelFrame(left_panels, text="Loot events", padding=6)
        left_panels.add(events_frame, weight=2)
        columns = ("idx", "time", "items", "loot", "cost", "return")
        self.loot_events_tree = ttk.Treeview(events_frame, columns=columns, show="headings", height=5)
        setup = [
            ("idx", "#", 45),
            ("time", "Log time", 155),
            ("items", "Items", 65),
            ("loot", "Loot PED", 80),
            ("cost", "Cost PED", 80),
            ("return", "Multiplier", 85),
        ]
        for col, title, width in setup:
            self.loot_events_tree.heading(col, text=title)
            self.loot_events_tree.column(col, width=width, anchor="center")
        self.make_tree_sortable(self.loot_events_tree, {col: title for col, title, _ in setup})
        events_y = ttk.Scrollbar(events_frame, orient="vertical", command=self.loot_events_tree.yview)
        events_x = ttk.Scrollbar(events_frame, orient="horizontal", command=self.loot_events_tree.xview)
        self.loot_events_tree.configure(yscrollcommand=events_y.set, xscrollcommand=events_x.set)
        self.loot_events_tree.grid(row=0, column=0, sticky="nsew")
        events_y.grid(row=0, column=1, sticky="ns")
        events_x.grid(row=1, column=0, sticky="ew")
        events_frame.rowconfigure(0, weight=1)
        events_frame.columnconfigure(0, weight=1)
        self.loot_events_tree.bind("<Double-1>", self.open_loot_event_details)
        ttk.Button(events_frame, text="Exclude selected events", command=self.exclude_selected_loot_events).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.loot_pager = PagedTree(self, self.loot_events_tree, events_frame)
        self.loot_pager.controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.loot_page_source = None

        graph1 = ttk.LabelFrame(graphs, text="1. Loot value / local time (% return)", padding=6)
        graph1.pack(fill="x", pady=(0, 6))
        self.loot_value_time_canvas = tk.Canvas(graph1, height=210, background=self.ui_colors["surface"], highlightthickness=1, highlightbackground=self.ui_colors["border"])
        self.loot_value_time_canvas.pack(fill="both", expand=True)

        graph2 = ttk.LabelFrame(graphs, text="2. Loot value / cost per kill (PED, multiplier)", padding=6)
        graph2.pack(fill="x", pady=6)
        self.loot_cost_canvas = tk.Canvas(graph2, height=210, background=self.ui_colors["surface"], highlightthickness=1, highlightbackground=self.ui_colors["border"])
        self.loot_cost_canvas.pack(fill="both", expand=True)

        graph3 = ttk.LabelFrame(graphs, text="3. Number of looted items / local time (not cumulative)", padding=6)
        graph3.pack(fill="x", pady=(6, 0))
        self.loot_items_time_canvas = tk.Canvas(graph3, height=210, background=self.ui_colors["surface"], highlightthickness=1, highlightbackground=self.ui_colors["border"])
        self.loot_items_time_canvas.pack(fill="both", expand=True)

        for canvas in (self.loot_value_time_canvas, self.loot_cost_canvas, self.loot_items_time_canvas):
            canvas.bind("<Configure>", lambda event, c=canvas: self.redraw_chart_canvas(c))
            canvas.bind("<ButtonPress-1>", lambda event, c=canvas: self.on_loot_chart_drag_start(c, event))
            canvas.bind("<B1-Motion>", lambda event, c=canvas: self.on_loot_chart_drag_motion(c, event))
            canvas.bind("<ButtonRelease-1>", lambda event, c=canvas: self.on_loot_chart_drag_end(c, event))

    def resize_loot_layout(self, event=None):
        height = 2 if self.root.winfo_height() < 800 else 3
        if int(self.loot_item_summary_tree.cget("height")) != height:
            self.loot_item_summary_tree.configure(height=height)

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

    def derived_cache(self):
        # Calculation helpers are also used without constructing Tk in tests.
        if not hasattr(self, "session_cache"):
            self.session_cache = SessionDerivedCache()
        return self.session_cache

    def session_loot_summary(self, session):
        if not session:
            return {"kills": 0, "loot_total": 0.0, "item_totals": {}, "item_values": {},
                    "excluded_events": 0, "excluded_items": 0}
        entry = self.derived_cache().entry(session)
        if entry["summary"] is None:
            rows = self.loot_events_for_session(session)
            raw = session.get("loot_events") or []
            entry["summary"] = {
                "kills": len(rows),
                "loot_total": sum(parse_float(row.get("value_ped"), 0.0) for row in rows),
                "item_totals": self.loot_item_totals(rows),
                "item_values": self.loot_item_value_totals(rows),
                "excluded_events": sum(bool(row.get("excluded_from_loot")) for row in raw),
                "excluded_items": sum(len(row.get("excluded_loot_items") or []) for row in raw),
            }
        return entry["summary"]

    def refresh_visible_session_views(self):
        """Hidden views read the current source on their next tab activation."""
        if self.is_tab_active(getattr(self, "session_details_tab", None)):
            self.show_session_details(self.selected_session_from_table())
        if self.is_tab_active(getattr(self, "loot_tab", None)):
            self.refresh_loot_tab(force=False)
        if self.is_tab_active(getattr(self, "mob_analysis_tab", None)):
            self.refresh_mob_analysis(persist_settings=False)

    def prune_session_cache(self):
        sources = [*self.sessions, *self.analysis_sessions]
        if self.current_session is not None:
            sources.append(vars(self.current_session))
        self.derived_cache().prune(sources)

    def reconstruct_loot_events_from_events(self, session):
        """Rebuild loot events from parsed events using the current grouping rules.

        Loot messages belong to one loot event only when they have the exact same
        chat.log timestamp (second precision) and there was no player attack
        between them. Any hit/crit/defense/miss starts the shot counter for the
        next kill, even when the next loot line has the same timestamp.
        """
        reconstructed = []
        shots_since_loot = 0
        count_hunting = bool(session.get("count_hunting", False))
        cost_per_shot = hunting_setup_cost_per_shot_ped(
            session.get("weapon", ""),
            session.get("amplifier", ""),
            session.get("attachments", []) or [],
        )

        for event in session.get("events", []) or []:
            etype = event.get("type", "")
            if etype in ("normal_hit", "crit", "defended_attack", "miss", "jammed"):
                shots_since_loot += 1
                continue
            if etype != "loot":
                continue

            item = event.get("item") or normalize_loot_item_name(
                str(event.get("message", "")).replace("You received", "").split("Value:")[0]
            )
            if ignored_loot_item_name(item):
                continue

            timestamp = str(event.get("timestamp", "") or "")
            continue_previous = bool(
                reconstructed
                and shots_since_loot == 0
                and timestamp
                and timestamp == str(reconstructed[-1].get("ended_at", "") or "")
            )

            if continue_previous:
                loot_event = reconstructed[-1]
            else:
                loot_event = {
                    "index": len(reconstructed) + 1,
                    "started_at": timestamp,
                    "ended_at": timestamp,
                    "value_ped": 0.0,
                    "cost_ped": (shots_since_loot * cost_per_shot) if count_hunting else 0.0,
                    "shots": shots_since_loot,
                    "items": {},
                    "messages": [],
                }
                reconstructed.append(loot_event)
                shots_since_loot = 0

            value_ped = float(event.get("value_ped", 0.0) or 0.0)
            quantity = int(event.get("quantity", 0) or 0)
            if quantity <= 1 and item in STACKABLE_ITEM_PED_VALUE:
                quantity = parse_loot_item_quantity(item, item, value_ped)
            if quantity <= 0:
                quantity = 1
            loot_event["ended_at"] = timestamp or loot_event.get("ended_at", "")
            loot_event["value_ped"] = float(loot_event.get("value_ped", 0.0) or 0.0) + value_ped
            loot_event.setdefault("items", {})[item] = int(loot_event.setdefault("items", {}).get(item, 0) or 0) + quantity
            loot_event.setdefault("messages", []).append(event.get("message", ""))

        return reconstructed

    def raw_loot_events_for_session(self, session):
        if not session:
            return []

        saved_events = list(session.get("loot_events", []) or [])
        try:
            grouping_version = int(session.get("loot_event_grouping_version", 0) or 0)
        except (TypeError, ValueError):
            grouping_version = 0

        # New sessions already store the corrected grouping/cost directly.
        if saved_events and (grouping_version >= LOOT_EVENT_GROUPING_VERSION
                             or any(row.get("excluded_from_loot") or row.get("excluded_loot_items") for row in saved_events)):
            return self.sanitize_loot_events(saved_events)

        # Older versions could merge loot lines across different seconds and
        # therefore assign several kills' shots to one later loot event. Rebuild
        # from the complete parsed-event history when it is available. To avoid
        # damaging very old sessions whose event history was truncated, only use
        # the rebuild when its total loot matches the saved loot-event total.
        reconstructed = self.reconstruct_loot_events_from_events(session)
        if reconstructed:
            if not saved_events:
                return reconstructed
            saved_sanitized = self.sanitize_loot_events(saved_events)
            saved_total = sum(float(row.get("value_ped", 0.0) or 0.0) for row in saved_sanitized)
            rebuilt_total = sum(float(row.get("value_ped", 0.0) or 0.0) for row in reconstructed)
            tolerance = max(1e-9, abs(saved_total) * 1e-9)
            if abs(saved_total - rebuilt_total) <= tolerance:
                return reconstructed
            return saved_sanitized

        if saved_events:
            return self.sanitize_loot_events(saved_events)
        return []

    def loot_events_for_session(self, session):
        if not session:
            return []
        return self.derived_cache().normalized(session, self.build_included_loot_events)

    def build_included_loot_events(self, session):
        """Read included loot without destroying original messages or amounts."""
        result = []
        excluded_cost = 0.0
        for row in self.raw_loot_events_for_session(session):
            if row.get("excluded_from_loot"):
                excluded_cost += parse_float(row.get("cost_ped"), 0.0)
                continue
            excluded_items = set(row.get("excluded_loot_items", []) or [])
            if excluded_items:
                details = self.loot_event_item_details(row)
                kept = {name: detail for name, detail in details.items() if name not in excluded_items}
                if not kept:
                    excluded_cost += parse_float(row.get("cost_ped"), 0.0)
                    continue
                row = {**row, "items": {name: detail["quantity"] for name, detail in kept.items()},
                       "value_ped": sum(detail["value_ped"] for detail in kept.values()),
                       "messages": [message for detail in kept.values() for message in detail["messages"]]}
            if excluded_cost:
                row = {**row, "cost_ped": parse_float(row.get("cost_ped"), 0.0) + excluded_cost}
                excluded_cost = 0.0
            result.append(row)
        if result and excluded_cost:
            result[-1] = {**result[-1], "cost_ped": parse_float(result[-1].get("cost_ped"), 0.0) + excluded_cost}
        return result

    @staticmethod
    def analysis_session_copy(session, previous=None):
        """Keep archive extensions while sharing all source session changes."""
        copied = json.loads(json.dumps(session, ensure_ascii=False))
        previous = previous or {}
        # Match raw event positions as well as their identity: multiple kills
        # may share the same second. Never attach another event's extension.
        old_events = previous.get("loot_events", []) or []
        if copied.get("loot_events_before_exclusion") and old_events:
            copied["loot_events_before_exclusion"] = json.loads(json.dumps(
                previous.get("loot_events_before_exclusion") or old_events, ensure_ascii=False))
        event_fields = {"index", "started_at", "ended_at", "value_ped", "cost_ped", "shots", "items", "messages",
                        "excluded_from_loot", "excluded_loot_items", "manual_cost_fraction"}
        for index, row in enumerate(copied.get("loot_events", []) or []):
            if index < len(old_events):
                old = old_events[index]
                same_event = old.get("started_at") == row.get("started_at") and old.get("items") == row.get("items")
                if (not same_event or any(key in old and key not in row for key in event_fields)) and not copied.get("loot_events_before_exclusion"):
                    copied["loot_events_before_exclusion"] = json.loads(json.dumps(
                        previous.get("loot_events_before_exclusion") or old_events, ensure_ascii=False))
                if same_event:
                    extensions = {key: value for key, value in old.items() if key not in event_fields}
                    copied["loot_events"][index] = {**extensions, **row}
        return {**previous, **copied}

    def analysis_sources(self, sessions):
        """Resolve links once; ambiguous legacy boundaries must never be guessed."""
        by_id = {str(row["id"]): row for row in sessions if isinstance(row, dict) and row.get("id")}
        def legacy_key(row):
            if not isinstance(row, dict) or row.get("id") or not row.get("started_at"):
                return None
            return tuple(row.get(key) for key in ("started_at", "ended_at", "start_offset", "end_offset"))
        # Legacy records without IDs can only be linked when their immutable
        # session boundaries identify exactly one record in each collection.
        legacy = {}
        for row in sessions:
            key = legacy_key(row)
            if key is not None:
                legacy.setdefault(key, []).append(row)
        archive_keys = [legacy_key(row) for row in self.analysis_sessions]
        archive_key_counts = {}
        for key in archive_keys:
            if key is not None:
                archive_key_counts[key] = archive_key_counts.get(key, 0) + 1
        result = []
        for row, key in zip(self.analysis_sessions, archive_keys):
            source = by_id.get(str(row.get("id"))) if isinstance(row, dict) and row.get("id") else None
            if source is None and key is not None and len(legacy.get(key, [])) == 1 and archive_key_counts[key] == 1:
                source = legacy[key][0]
            result.append(source)
        return result

    @staticmethod
    def analysis_source_is_current(source, archived):
        # Compare original fields, allowing archive-only extensions. This avoids
        # serialization/deep-copy of every already synchronized history at
        # startup. Stale standard event fields still require a full repair.
        for key, value in source.items():
            if key not in ("loot_events", "loot_events_before_exclusion") and (key not in archived or archived[key] != value):
                return False
        if source.get("loot_events_before_exclusion") and not archived.get("loot_events_before_exclusion"):
            return False
        rows, old_rows = source.get("loot_events") or [], archived.get("loot_events") or []
        if len(rows) != len(old_rows):
            return False
        standard = {"index", "started_at", "ended_at", "value_ped", "cost_ped", "shots", "items", "messages",
                    "excluded_from_loot", "excluded_loot_items", "manual_cost_fraction"}
        for row, old in zip(rows, old_rows):
            if any(key not in old or old[key] != value for key, value in row.items()):
                return False
            if any(key in old and key not in row for key in standard):
                return False
        return True

    def synchronized_analysis_sessions(self, sessions, *, changed_only=False):
        unchanged = {id(row) for row in self.sessions} if changed_only else set()
        return [self.analysis_session_copy(source, archived)
                if source is not None and id(source) not in unchanged and not self.analysis_source_is_current(source, archived)
                else archived
                for source, archived in zip(self.analysis_sources(sessions), self.analysis_sessions)]

    def save_session_updates(self, staged, *, loot_changed=True):
        """Persist edits and existing analysis copies; roll back on write failure."""
        analysis = self.synchronized_analysis_sessions(staged, changed_only=True)
        try:
            save_json(SESSIONS_FILE, staged)
        except OSError as error:
            messagebox.showerror("Sessions not saved", str(error))
            return False
        if analysis != self.analysis_sessions:
            try:
                save_json(ANALYSIS_SESSIONS_FILE, analysis)
            except OSError as error:
                try:
                    save_json(SESSIONS_FILE, self.sessions)
                except OSError as rollback_error:
                    messagebox.showerror("Session files need attention", f"Analysis save failed: {error}. "
                                         f"Restoring sessions also failed: {rollback_error}. Check the session files and their backups.")
                    return False
                messagebox.showerror("Changes not saved", f"Analysis could not be saved: {error}. The session edit was undone; retry after fixing the write error.")
                return False
        if not loot_changed:
            cache = self.derived_cache()
            for original, updated in zip(self.sessions, staged):
                if updated is not original:
                    cache.inherit_summary(original, updated)
            for source, original, updated in zip(self.analysis_sources(staged), self.analysis_sessions, analysis):
                if updated is not original:
                    # Use the canonical source totals: an archive loaded from
                    # disk can be stale even during a metadata/cost-only edit.
                    cache.inherit_summary(source, updated)
        self.analysis_sessions = analysis
        return True

    def exclude_selected_loot_events(self):
        rows = [self.loot_event_iid_to_event[iid] for iid in self.loot_events_tree.selection()
                if iid in self.loot_event_iid_to_event]
        if not rows:
            messagebox.showwarning("No loot selected", "Select one or more loot events first. Double-click an event to exclude a specific item.")
            return
        self.change_loot_exclusions(self.loot_source_session(), [row["index"] for row in rows])

    def restore_excluded_loot(self):
        self.change_loot_exclusions(self.loot_source_session(), restore=True)

    def change_loot_exclusions(self, source, event_indices=(), item_name=None, *, restore=False):
        """Exclude whole receipts or one item in selected receipts, reversibly."""
        if not source:
            return False
        live = self.current_session if self.current_session is not None and source is vars(self.current_session) else None
        # loot_source_session returns vars(current_session), with its identity.
        saved_index = next((i for i, row in enumerate(self.sessions) if row is source), None)
        if live is None and saved_index is None:
            messagebox.showwarning("Session unavailable", "Select the session again before changing loot.")
            return False
        raw = self.raw_loot_events_for_session(source)
        selected = set(event_indices)
        changed = False
        for row in raw:
            if restore:
                if row.pop("excluded_from_loot", None) or row.get("excluded_loot_items"):
                    changed = True
                row.pop("excluded_loot_items", None)
            elif row["index"] in selected:
                if item_name is not None:
                    if item_name not in row.get("items", {}):
                        continue
                    if len(row["items"]) > 1 and not all(detail["messages"] for detail in self.loot_event_item_details(row).values()):
                        messagebox.showwarning("Item value unavailable", "This older mixed loot event has no exact item values. Exclude the whole event instead.")
                        return False
                    row["excluded_loot_items"] = sorted(set(row.get("excluded_loot_items", []) or []) | {item_name})
                else:
                    row["excluded_from_loot"] = True
                changed = True
        if not changed:
            return False
        updated = {**source, "loot_events": raw, "loot_event_grouping_version": LOOT_EVENT_GROUPING_VERSION}
        if not updated.get("loot_events_before_exclusion") and source.get("loot_events"):
            updated["loot_events_before_exclusion"] = json.loads(json.dumps(source["loot_events"], ensure_ascii=False))
        included = self.loot_events_for_session(updated)
        updated["loot_ped_total"] = sum(row["value_ped"] for row in included)
        updated["loot_event_count"] = len(included)
        if live is not None:
            # Live sessions are saved by stop_sync/on_close; keep dataclass
            # fields so exclusions survive the normal session save path.
            for key in ("loot_events", "loot_event_grouping_version", "loot_ped_total", "loot_event_count", "loot_events_before_exclusion"):
                setattr(live, key, updated[key])
            self.derived_cache().invalidate(vars(live))
            live._has_loot_exclusions = any(row.get("excluded_from_loot") or row.get("excluded_loot_items") for row in raw)
            self.live_ui_dirty = True
            self.refresh_live_ui(force=True)
        else:
            staged = list(self.sessions)
            staged[saved_index] = updated
            if not self.save_session_updates(staged):
                self.prune_session_cache()
                return False
            source.update(updated)
            self.derived_cache().invalidate(source)
            self.refresh_sessions_table()
            iid = f"session_{saved_index}"
            if self.sessions_tree.exists(iid):
                self.sessions_tree.selection_set(iid)
                self.sessions_tree.focus(iid)
            self.refresh_visible_session_views()
        self.prune_session_cache()
        self.reset_loot_zoom()
        return True

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
            if "messages" in event:
                event["messages"] = messages
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
        detail_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=8)
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
        self.pack_table(detail_tree, table_frame)

        source = self.loot_source_session()
        def exclude_item():
            selected = detail_tree.selection()
            if not selected:
                messagebox.showwarning("No item selected", "Select an item in this event first.", parent=window)
                return
            name = detail_tree.item(selected[0], "values")[0]
            if self.change_loot_exclusions(source, [event_index], name):
                window.destroy()
        actions = ttk.Frame(window, padding=(10, 0, 10, 6))
        actions.pack(fill="x", before=table_frame)
        ttk.Button(actions, text="Exclude selected item from this event", command=exclude_item).pack(side="left")
        ttk.Label(actions, text="Original loot is kept. Use Restore excluded loot to undo.").pack(side="left", padx=10)

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
        self.style_tracker_widgets(window)

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

        def skill_message_values(pair):
            iid, row = pair
            index = int(iid.replace("gain_", "")) + 1
            return (
                    index,
                    row.get("timestamp", ""),
                    f"{float(row.get('point_gain', 0.0) or 0.0):.6f}",
                    f"{float(row.get('tt_gain', 0.0) or 0.0):.8f}",
                    f"{float(row.get('old_points', 0.0) or 0.0):.6f}",
                    f"{float(row.get('new_points', 0.0) or 0.0):.6f}",
                    row.get("message", ""),
            )

        self.make_tree_sortable(detail_tree, {column: title for column, title, _ in setup})
        pager = PagedTree(self, detail_tree, table_frame)
        pager.controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        pager.set_rows([(f"gain_{i}", row) for i, row in enumerate(details)], skill_message_values)

        if not details:
            detail_tree.insert("", "end", values=("", "", "", "", "", "", "No saved gain messages for this skill."))
        self.style_tracker_widgets(window)

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

        drop_indices = {}
        def drop_values(pair):
            _iid, row = pair
            value_ped = float(row.get("value_ped", 0.0) or 0.0)
            event_value = float(row.get("event_value_ped", 0.0) or 0.0)
            item_after_mu = self.loot_value_after_markup(
                item_name, value_ped, int(row.get("quantity", 0) or 0)
            )
            messages = " | ".join(str(message) for message in row.get("messages", []) if message)
            if not messages:
                messages = "Original message unavailable for this older saved event."
            return (
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
            )

        self.make_tree_sortable(detail_tree, {column: title for column, title, _ in setup})
        pager = PagedTree(self, detail_tree, table_frame)
        pager.controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        def map_drops(visible):
            drop_indices.clear()
            drop_indices.update({iid: row["event_index"] for iid, row in visible})
        pager.set_rows([(f"drop_{i}", row) for i, row in enumerate(drops)], drop_values, on_render=map_drops)

        def exclude_drops():
            indices = [drop_indices[iid] for iid in detail_tree.selection() if iid in drop_indices]
            if not indices:
                messagebox.showwarning("No drops selected", "Select the unwanted drops first.", parent=window)
                return
            if self.change_loot_exclusions(session, indices, item_name):
                window.destroy()
        actions = ttk.Frame(window, padding=(10, 0, 10, 8))
        actions.pack(fill="x", before=table_frame)
        ttk.Button(actions, text="Exclude selected drops of this item", command=exclude_drops).pack(side="left")
        ttk.Label(actions, text="Only selected receipts change; other drops of this item stay included.").pack(side="left", padx=10)

        if not drops:
            detail_tree.insert("", "end", values=("", "", "", "", "", "", "", "", "", "No saved drops found for this item."))
        self.style_tracker_widgets(window)

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
        self.refresh_loot_tab(force=False)
        self.refresh_visible_session_views()

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
        self.refresh_loot_tab(force=False)

    def refresh_loot_item_checks(self, force=False, *, totals=None):
        """Rebuild the loot item checkbox list only when it actually changed.

        Recreating dozens/hundreds of Tk checkboxes on every parsed log batch is
        expensive and was one of the main reasons the tracker UI felt slow.
        """
        session = self.loot_source_session()
        if totals is None:
            totals = self.session_loot_summary(session)["item_totals"]
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
                style="Tracker.Surface.TCheckbutton",
                command=lambda: self.refresh_loot_tab(force=False),
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
            self.refresh_loot_tab(force=False)
            self.last_loot_live_refresh_at = time.monotonic()

    def first_loot_event_time(self, loot_events):
        times = [self.loot_event_chart_time(event) for event in loot_events]
        return min((value for value in times if value is not None), default=None)

    def loot_event_chart_time(self, loot_event):
        """Read legacy naive chat.log timestamps as UTC without changing them."""
        for key in ("ended_at", "started_at"):
            value = parse_any_timestamp(loot_event.get(key))
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)
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

    def refresh_loot_tab(self, *, force=True):
        if not hasattr(self, "loot_value_time_canvas"):
            return
        self.reload_market_data_if_changed()
        session = self.loot_source_session()
        if force and session:
            self.derived_cache().invalidate(session)
        summary = self.session_loot_summary(session)
        generation = self.derived_cache().entry(session)["generation"] if session else None
        signature = (id(session), generation, (session or {}).get("ped_cycled"),
                     (session or {}).get("damage_total"), (session or {}).get("mob"),
                     (session or {}).get("maturity"), repr(self.loot_markups),
                     repr(self.market_weekly_markups), tuple(self.selected_loot_items()))
        if not force and signature == self.loot_view_signature:
            return
        loot_events = self.loot_events_for_session(session)
        excluded_events = summary["excluded_events"]
        excluded_items = summary["excluded_items"]
        self.restore_loot_button.configure(state="normal" if excluded_events or excluded_items else "disabled")
        self.refresh_loot_item_checks(totals=summary["item_totals"])
        reset_page = session is not self.loot_page_source
        self.loot_page_source = session
        rows_signature = (id(session), generation)
        if force or rows_signature != self.loot_rows_signature:
            self.loot_pager.set_rows([(f"loot_event_{i}", row) for i, row in enumerate(loot_events)],
                                     self.loot_event_table_values, reset=reset_page,
                                     on_render=lambda rows: setattr(self, "loot_event_iid_to_event", dict(rows)))
            self.loot_rows_signature = rows_signature
        self.loot_view_signature = signature

        if not session:
            self.loot_chart_signature = self.loot_base_chart_signature = None
            self.loot_plot_data = None
            self.loot_chart_payloads.clear()
            self.loot_summary_var.set("No active or saved session yet.")
            if hasattr(self, "loot_item_summary_tree"):
                self.loot_item_summary_tree.delete(*self.loot_item_summary_tree.get_children())
                self.loot_item_summary_iid_to_name = {}
            self.clear_chart(self.loot_value_time_canvas, "No loot data")
            self.clear_chart(self.loot_cost_canvas, "No loot data")
            self.clear_chart(self.loot_items_time_canvas, "No loot data")
            return

        ped_cycled = float(session.get("ped_cycled", 0.0) or 0.0)
        loot_total = summary["loot_total"]
        cost_per_kill = ped_cycled / len(loot_events) if loot_events else 0.0
        item_totals = summary["item_totals"]
        item_value_totals = summary["item_values"]
        loot_after_mu = self.refresh_loot_item_summary_table(item_value_totals, loot_total)
        known_item_tt = sum(float(row.get("value_ped", 0.0) or 0.0) for row in item_value_totals.values())
        has_item_values = math.isclose(known_item_tt, loot_total, rel_tol=1e-9, abs_tol=1e-9)
        average_mu = f"{percent(loot_after_mu, loot_total):.2f}%" if loot_total > 0 and has_item_values else "—"
        combat = self.calculate_session_combat_metrics(session)
        dpp = f'{combat["dpp"]:.3f}' if combat["dpp"] is not None else "—"
        effective_dpp = f'{combat["effective_dpp"]:.3f}' if combat["effective_dpp"] is not None else "—"
        source = "active session" if self.current_session is not None else "saved session"
        self.loot_summary_var.set(
            f"Source: {source} | Loot events/kills: {len(loot_events)} | PED cycled: {ped_cycled:.4f} | "
            f"TT loot: {loot_total:.4f} PED ({percent(loot_total, ped_cycled):.2f}%) | "
            f"Loot after MU: {loot_after_mu:.4f} PED ({percent(loot_after_mu, ped_cycled):.2f}% of cycled)\n"
            f"Average overall MU (TT-weighted): {average_mu} | "
            f"DPP: {dpp} | Effective DPP: {effective_dpp} (damage/PEC)\n"
            f"Cost per kill/event: {cost_per_kill:.6f} PED | Unique items: {len(item_value_totals)} | "
            f"Excluded events: {excluded_events} | Excluded item drops: {excluded_items}"
        )

        chart_signature = (id(session), generation, ped_cycled, tuple(self.selected_loot_items()))
        if not force and chart_signature == self.loot_chart_signature:
            return
        self.loot_chart_signature = chart_signature

        base_signature = (id(session), generation, ped_cycled)
        if force or base_signature != self.loot_base_chart_signature:
            # Use the first real loot timestamp, not the app session start time.
            # Resyncing historical logs can give a session start later than its
            # loot. Retain relative minutes for sampling/zoom; the display-only UTC
            # origin converts tick labels to the PC's local clock, including DST.
            event_times = [self.loot_event_chart_time(event) for event in loot_events]
            time_labels = [self.format_chart_time_label(value, index) for index, value in enumerate(event_times, start=1)]
            base_time = min((value for value in event_times if value is not None), default=None)
            has_real_time_axis = base_time is not None

            # Graph 1: cumulative loot return over actual loot time.
            cumulative_loot = 0.0
            cumulative_cost = 0.0
            loot_value_points = []
            for index, (loot_event, event_time) in enumerate(zip(loot_events, event_times), start=1):
                value_ped = float(loot_event.get("value_ped", 0.0) or 0.0)
                event_cost = float(loot_event.get("cost_ped", 0.0) or 0.0)
                if event_cost <= 0 and cost_per_kill > 0:
                    event_cost = cost_per_kill
                cumulative_loot += value_ped
                cumulative_cost += event_cost
                if has_real_time_axis and event_time is None:
                    # Include unknown-time loot in totals, but do not invent a time
                    # for its plot point. Entirely undated sessions use event #.
                    continue
                x_value = self.elapsed_minutes(base_time, event_time, index) if has_real_time_axis else float(index)
                denominator = cumulative_cost if cumulative_cost > 0 else ped_cycled
                loot_value_points.append({
                    "x": x_value,
                    "y": percent(cumulative_loot, denominator),
                    "label": time_labels[index - 1] if has_real_time_axis else str(index),
                })
            self.render_line_chart(
                self.loot_value_time_canvas,
                self.downsample_time_points_by_minute(loot_value_points, bucket_minutes=1.0),
                title="Cumulative loot / cumulative cost",
                x_label="Loot time (local PC time)" if has_real_time_axis else "Loot event #",
                y_label="Loot return",
                x_is_time=has_real_time_axis,
                time_origin=base_time,
                y_suffix="%",
                smooth=False,
                y_reference_lines=[(100.0, "100%")],
            )

            scatter_points = []
            for index, (loot_event, event_time) in enumerate(zip(loot_events, event_times), start=1):
                cost_ped = float(loot_event.get("cost_ped", 0.0) or 0.0)
                value_ped = float(loot_event.get("value_ped", 0.0) or 0.0)
                scatter_points.append({
                    "x": cost_ped,
                    "y": value_ped,
                    "label": time_labels[index - 1],
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

            self.loot_plot_data = (event_times, base_time, has_real_time_axis, time_labels)
            self.loot_base_chart_signature = base_signature
        else:
            event_times, base_time, has_real_time_axis, time_labels = self.loot_plot_data

        selected_items = self.selected_loot_items()
        if not selected_items:
            selected_items = list(item_totals.keys())[:5]
        item_series = []
        for item in selected_items[:12]:
            raw_points = []
            for index, (loot_event, event_time) in enumerate(zip(loot_events, event_times), start=1):
                quantity = int((loot_event.get("items", {}) or {}).get(item, 0) or 0)
                if quantity <= 0:
                    continue
                if has_real_time_axis and event_time is None:
                    continue
                raw_points.append({
                    "x": self.elapsed_minutes(base_time, event_time, index) if has_real_time_axis else float(index),
                    "y": quantity,
                    "label": time_labels[index - 1] if has_real_time_axis else str(index),
                })
            if raw_points:
                if has_real_time_axis:
                    buckets = {}
                    for point in raw_points:
                        bucket = int(float(point.get("x", 0.0)) // 1.0)
                        if bucket not in buckets:
                            buckets[bucket] = {"x": float(bucket), "y": 0, "label": self._time_axis_label(float(bucket), time_origin=base_time)}
                        buckets[bucket]["y"] += int(point.get("y", 0) or 0)
                    points = list(buckets.values())
                else:
                    points = raw_points
                item_series.append((item, self.downsample_points(points, 800)))
        self.render_multi_point_chart(
            self.loot_items_time_canvas,
            item_series,
            title="Looted item quantity by time",
            x_label="Loot time (local PC time)" if has_real_time_axis else "Loot event #",
            y_label="Items looted",
            x_is_time=has_real_time_axis,
            time_origin=base_time,
        )

    @staticmethod
    def loot_event_table_values(pair):
        _iid, event = pair
        cost = parse_float(event.get("cost_ped"), 0.0)
        value = parse_float(event.get("value_ped"), 0.0)
        return (event.get("index", ""), event.get("ended_at", event.get("started_at", "")),
                sum(int(v or 0) for v in (event.get("items") or {}).values()),
                f"{value:.4f}", f"{cost:.4f}", f"{value / cost:.2f}x" if cost else "")

    def clear_chart(self, canvas, text="No data"):
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 170)
        canvas.create_rectangle(0, 0, width, height, fill=self.ui_colors["surface"], outline="")
        canvas.create_text(width / 2, height / 2, text=text, fill=self.ui_colors["muted"])

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
        self.redraw_all_loot_charts()
        self.loot_view_signature = None
        self.refresh_visible_session_views()

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
        rect_id = canvas.create_rectangle(min(x1, x2), top, max(x1, x2), bottom, outline=self.ui_colors["accent"], fill=self.ui_colors["selection_preview"], stipple="gray25")
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

    def _selection_time_text(self, min_x, max_x, x_is_time, time_origin=None):
        if x_is_time:
            include_date = self.chart_range_crosses_date(time_origin, min_x, max_x)
            start = self._time_axis_label(min_x, time_origin=time_origin, include_date=include_date)
            end = self._time_axis_label(max_x, time_origin=time_origin, include_date=include_date)
            return f"{start} - {end} ({self._time_axis_label(max_x - min_x)})"
        return f"{min_x:.4f} - {max_x:.4f}"

    def describe_loot_chart_selection(self, canvas, min_x, max_x):
        payload = self.loot_chart_payloads.get(canvas) or {}
        kind = payload.get("kind")
        x_is_time = bool(payload.get("x_is_time", False))
        range_text = self._selection_time_text(min_x, max_x, x_is_time, payload.get("time_origin"))
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
        return self.chart_local_time(event_time, 0).strftime("%H:%M:%S")

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

    def chart_local_time(self, time_origin, minutes_value):
        if time_origin.tzinfo is None:
            time_origin = time_origin.replace(tzinfo=timezone.utc)
        # Add elapsed time in UTC before converting: local DST transitions do
        # not change event spacing or the actual instant represented by a tick.
        return (time_origin.astimezone(timezone.utc) + timedelta(minutes=float(minutes_value))).astimezone()

    def chart_range_crosses_date(self, time_origin, min_x, max_x):
        return bool(time_origin is not None and
                    self.chart_local_time(time_origin, min_x).date() != self.chart_local_time(time_origin, max_x).date())

    def _time_axis_label(self, minutes_value, *, time_origin=None, include_date=False):
        if time_origin is not None:
            local_time = self.chart_local_time(time_origin, minutes_value)
            clock = local_time.strftime("%H:%M:%S" if local_time.second else "%H:%M")
            return f'{local_time:%d.%m} {clock}' if include_date else clock
        minutes_value = float(minutes_value)
        total_seconds = int(round(minutes_value * 60.0))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _draw_xy_axes(self, canvas, title, x_label, y_label, min_x, max_x, min_y, max_y, *, x_is_time=False, y_suffix="", time_origin=None):
        width, height, left, top, right, bottom = self._chart_area(canvas)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=self.ui_colors["surface"], outline="")
        axis_color = self.ui_colors["axis"]
        grid_color = self.ui_colors["grid"]
        text_color = self.ui_colors["ink"]
        muted_color = self.ui_colors["muted"]

        canvas.create_text(width / 2, 14, text=title, fill=text_color, font=(self.ui_font_family, 11, "bold"))
        canvas.create_line(left, bottom, right, bottom, fill=axis_color)
        canvas.create_line(left, top, left, bottom, fill=axis_color)

        x_range = max(max_x - min_x, 1e-9)
        y_range = max(max_y - min_y, 1e-9)

        for i in range(5):
            frac = i / 4
            y = bottom - (bottom - top) * frac
            y_value = min_y + y_range * frac
            canvas.create_line(left, y, right, y, fill=grid_color)
            canvas.create_text(left - 6, y, anchor="e", text=self._format_axis_value(y_value, y_suffix), fill=muted_color, font=(self.ui_font_family, 8))

        include_date = x_is_time and self.chart_range_crosses_date(time_origin, min_x, max_x)
        tick_count = 4 if include_date and right - left < 800 else 6
        for i in range(tick_count):
            frac = i / (tick_count - 1)
            x = left + (right - left) * frac
            x_value = min_x + x_range * frac
            canvas.create_line(x, top, x, bottom, fill=grid_color)
            label = self._time_axis_label(x_value, time_origin=time_origin, include_date=include_date) if x_is_time else self._format_axis_value(x_value)
            # Long dated labels stay within the canvas at the two edge ticks.
            anchor = "nw" if i == 0 else "ne" if i == tick_count - 1 else "n"
            canvas.create_text(x, bottom + 14, anchor=anchor, text=label, fill=muted_color, font=(self.ui_font_family, 8))

        canvas.create_text((left + right) / 2, height - 8, text=x_label, fill=text_color, font=(self.ui_font_family, 9))
        canvas.create_text(14, (top + bottom) / 2, text=y_label, fill=text_color, font=(self.ui_font_family, 9), angle=90)
        return width, height, left, top, right, bottom

    def _project_point(self, x, y, left, top, right, bottom, min_x, max_x, min_y, max_y):
        x_range = max(max_x - min_x, 1e-9)
        y_range = max(max_y - min_y, 1e-9)
        px = left + (right - left) * ((float(x) - min_x) / x_range)
        py = bottom - (bottom - top) * ((float(y) - min_y) / y_range)
        return px, py

    def render_line_chart(self, canvas, points, *, title, x_label, y_label, x_is_time=True, y_suffix="", smooth=False, y_reference_lines=None, time_origin=None):
        payload = {
            "kind": "line",
            "points": list(points or []),
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "time_origin": time_origin,
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
            time_origin=payload.get("time_origin"),
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
                canvas.create_line(x1, y1, x2, y2, fill=self.ui_colors["positive"], dash=(5, 4), width=2)
                label_x = max(left + 36, min(right - 6, x2 - 6))
                label_y = max(top + 10, min(bottom - 10, y2 - 8))
                canvas.create_text(label_x, label_y, anchor="e", text=str(reference_label), fill=self.ui_colors["positive"], font=(self.ui_font_family, 9, "bold"))
        coords = []
        for point in points:
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            coords.extend([px, py])
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=self.ui_colors["series"][0], width=2, smooth=bool(payload.get("smooth", False)))
        # Drawing an oval for every point is expensive with long sessions.
        dot_stride = max(1, len(points) // 250)
        for idx, point in enumerate(points):
            if idx % dot_stride != 0 and idx != len(points) - 1:
                continue
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=self.ui_colors["series"][0], outline="")

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
                canvas.create_line(x1, y1, x2, y2, fill=self.ui_colors["positive"], dash=(5, 4), width=width)
                label = "1.0x" if multiplier == 1.0 else f"x{multiplier:g}"
                label_y = max(top + 10, min(bottom - 10, y2 - 7))
                canvas.create_text(right - 6, label_y, anchor="e", text=label, fill=self.ui_colors["positive"], font=(self.ui_font_family, 9, "bold"))
        for point in points:
            px, py = self._project_point(point.get("x", 0.0), point.get("y", 0.0), left, top, right, bottom, min_x, max_x, min_y, max_y)
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill=self.ui_colors["series"][0], outline="")

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
        canvas.create_rectangle(box_left, box_top, box_right, box_bottom, fill=self.ui_colors["surface"], outline=self.ui_colors["border"])
        y = box_top + 12
        for name, color in visible_entries:
            if y > box_bottom - 4:
                break
            text = str(name)[:24]
            canvas.create_line(box_left + 8, y, box_left + 22, y, fill=color, width=3)
            canvas.create_text(box_left + 28, y, anchor="w", text=text, fill=self.ui_colors["ink"], font=(self.ui_font_family, 9, "bold"))
            y += line_height

    def render_multi_line_chart(self, canvas, series, *, title, x_label, y_label, x_is_time=True, y_suffix="", smooth=False, time_origin=None):
        payload = {
            "kind": "multi_line",
            "series": [(name, list(points or [])) for name, points in list(series or [])],
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "time_origin": time_origin,
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
            time_origin=payload.get("time_origin"),
        )
        palette = self.ui_colors["series"]
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

    def render_multi_point_chart(self, canvas, series, *, title, x_label, y_label, x_is_time=True, time_origin=None):
        payload = {
            "kind": "multi_points",
            "series": [(name, list(points or [])) for name, points in list(series or [])],
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x_is_time": bool(x_is_time),
            "time_origin": time_origin,
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
            time_origin=payload.get("time_origin"),
        )
        self._remember_chart_meta(canvas, left=left, top=top, right=right, bottom=bottom, min_x=min_x, max_x=max_x, kind="multi_points")
        palette = self.ui_colors["series"]
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
        content = self.scrollable_tab_content(self.hunting_tab)
        columns = ttk.Frame(content)
        columns.pack(fill="x", padx=10, pady=10)
        columns.columnconfigure(0, weight=1, uniform="hunting_columns")
        columns.columnconfigure(1, weight=1, uniform="hunting_columns")
        equipment = ttk.Frame(columns)
        equipment.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        targets = ttk.Frame(columns)
        targets.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        profiles = ttk.LabelFrame(equipment, text="Equipment setups", padding=10)
        profiles.pack(fill="x")
        ttk.Label(profiles, text="Click a setup to use its equipment. Your mob stays unchanged.", wraplength=400, justify="left").pack(fill="x")
        self.hunting_setups_tree = ttk.Treeview(profiles, columns=("name", "weapon"), show="headings", height=6, selectmode="browse")
        for column, title, width in (("name", "Setup", 160), ("weapon", "Weapon", 240)):
            self.hunting_setups_tree.heading(column, text=title)
            self.hunting_setups_tree.column(column, width=width, anchor="w")
        self.pack_table(self.hunting_setups_tree, profiles, pady=(6, 8))
        self.hunting_setups_tree.bind("<ButtonRelease-1>", self.use_selected_hunting_setup)
        self.hunting_setups_tree.bind("<Return>", self.use_selected_hunting_setup)
        self.hunting_setups_tree.bind("<space>", self.use_selected_hunting_setup)
        editor = ttk.Frame(profiles)
        editor.pack(fill="x")
        ttk.Label(editor, text="Name:").pack(side="left")
        ttk.Entry(editor, textvariable=self.hunting_setup_name_var, width=20).pack(side="left", fill="x", expand=True, padx=(8, 0))
        actions = ttk.Frame(profiles)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Use selected", command=self.use_selected_hunting_setup).pack(side="left")
        ttk.Button(actions, text="Save / Update", command=self.save_named_hunting_setup).pack(side="left", padx=8)
        ttk.Button(actions, text="Delete", command=self.delete_named_hunting_setup).pack(side="left")
        self.add_wrapped_label(profiles, self.hunting_setup_status_var)
        self.refresh_hunting_setup_values()

        favorites = ttk.LabelFrame(targets, text="Favorite mobs", padding=10)
        favorites.pack(fill="x")
        ttk.Label(favorites, text="Click a favorite to use its mob and maturity. Equipment stays unchanged.", wraplength=400, justify="left").pack(fill="x")
        self.favorite_mobs_tree = ttk.Treeview(favorites, columns=("mob", "maturity"), show="headings", height=6, selectmode="browse")
        for column, title, width in (("mob", "Mob", 240), ("maturity", "Maturity", 150)):
            self.favorite_mobs_tree.heading(column, text=title)
            self.favorite_mobs_tree.column(column, width=width, anchor="w")
        self.pack_table(self.favorite_mobs_tree, favorites, pady=(6, 8))
        self.favorite_mobs_tree.bind("<ButtonRelease-1>", self.use_selected_favorite_mob)
        self.favorite_mobs_tree.bind("<Return>", self.use_selected_favorite_mob)
        self.favorite_mobs_tree.bind("<space>", self.use_selected_favorite_mob)
        actions = ttk.Frame(favorites)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Use selected", command=self.use_selected_favorite_mob).pack(side="left")
        ttk.Button(actions, text="Remove selected", command=self.remove_selected_favorite_mob).pack(side="left", padx=8)
        self.add_wrapped_label(favorites, self.favorite_mob_status_var)
        self.refresh_favorite_mobs()

        frame = ttk.LabelFrame(equipment, text="Equipment", padding=10)
        frame.pack(fill="x", pady=(10, 0))
        frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(frame, text="Count hunting / PED cycled during sync", variable=self.count_hunting_var, command=self.on_hunting_changed).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        search_fields = (
            ("Weapon", self.weapon_filter_var, self.filter_weapon_values, self.clear_weapon_filter),
            ("Amplifier", self.amplifier_filter_var, self.filter_amplifier_values, self.clear_amplifier_filter),
            ("Attachment", self.attachment_filter_var, self.filter_attachment_values, self.clear_attachment_filter),
        )
        for row, (label, variable, callback, clear) in zip((1, 3, 5), search_fields):
            ttk.Label(frame, text=f"{label} search:").grid(row=row, column=0, sticky="w")
            search = ttk.Entry(frame, textvariable=variable, width=20)
            search.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
            search.bind("<KeyRelease>", lambda event, var=variable, callback=callback: callback(var.get()))
            ttk.Button(frame, text="Clear", command=clear).grid(row=row, column=2, padx=4)
        ttk.Label(frame, text="Weapon:").grid(row=2, column=0, sticky="w")
        self.weapon_combo = ttk.Combobox(frame, textvariable=self.weapon_var, values=self.all_weapon_names, width=25)
        self.weapon_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        self.weapon_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.weapon_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        self.weapon_combo.bind("<KeyRelease>", lambda e: self.filter_weapon_values(self.weapon_var.get()))
        ttk.Label(frame, text="Amplifier:").grid(row=4, column=0, sticky="w")
        self.amplifier_combo = ttk.Combobox(frame, textvariable=self.amplifier_var, values=[""] + self.all_amplifier_names, width=25)
        self.amplifier_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        self.amplifier_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.amplifier_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        self.amplifier_combo.bind("<KeyRelease>", lambda e: self.filter_amplifier_values(self.amplifier_var.get()))
        self.attachment_combos = []
        for index, attachment_var in enumerate(self.attachment_vars, start=1):
            ttk.Label(frame, text=f"Attachment {index}:").grid(row=5+index, column=0, sticky="w")
            combo = ttk.Combobox(frame, textvariable=attachment_var, values=[""] + self.all_attachment_names, width=25)
            combo.grid(row=5+index, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
            combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
            combo.bind("<KeyRelease>", lambda e, var=attachment_var: self.filter_attachment_values(var.get()))
            self.attachment_combos.append(combo)
        ttk.Label(frame, textvariable=self.weapon_cost_var, style="Tracker.Emphasis.TLabel").grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))

        target = ttk.LabelFrame(targets, text="Hunting target", padding=10)
        target.pack(fill="x", pady=(10, 0))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="Mob search:").grid(row=0, column=0, sticky="w")
        mob_search = ttk.Entry(target, textvariable=self.mob_filter_var, width=20)
        mob_search.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        mob_search.bind("<KeyRelease>", lambda e: self.filter_mob_values(self.mob_filter_var.get()))
        ttk.Button(target, text="Clear", command=self.clear_mob_filter).grid(row=0, column=2, padx=4)
        ttk.Label(target, text="Mob:").grid(row=1, column=0, sticky="w")
        self.mob_combo = ttk.Combobox(target, textvariable=self.mob_var, values=self.all_mob_names, width=25)
        self.mob_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        self.mob_combo.bind("<<ComboboxSelected>>", lambda e: self.on_mob_changed())
        self.mob_combo.bind("<FocusOut>", lambda e: self.on_mob_changed())
        self.mob_combo.bind("<KeyRelease>", lambda e: self.filter_mob_values(self.mob_var.get()))
        ttk.Label(target, text="Maturity:").grid(row=2, column=0, sticky="w")
        self.maturity_combo = ttk.Combobox(target, textvariable=self.maturity_var, values=[], width=25)
        self.maturity_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        self.maturity_combo.bind("<<ComboboxSelected>>", lambda e: self.on_hunting_changed())
        self.maturity_combo.bind("<FocusOut>", lambda e: self.on_hunting_changed())
        info = ttk.Label(target, textvariable=self.mob_info_var, wraplength=400, justify="left")
        info.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        info.bind("<Configure>", lambda e: info.configure(wraplength=max(1, e.width)))
        ttk.Button(target, text="Add current mob + maturity to favorites", command=self.add_current_favorite_mob).grid(row=4, column=0, columnspan=3, sticky="ew")
        self.update_maturity_values()
        help_box = ttk.LabelFrame(targets, text="Counting rule", padding=10)
        help_box.pack(fill="x", pady=10)
        ttk.Label(help_box, text=(
            "PED cycled increments on every player attack: normal hit, critical hit, target Jammed/Evaded/Dodged, or 'You missed'.\n"
            "Jammed, Evaded, and Dodged are one target-defense category; 'You missed' is counted separately.\n"
            "It ignores enemy attacks like 'The attack missed you' and 'You took damage'.\n"
            "Per attack cost is calculated from weapon, amplifier, and attachment decay / 100 + ammo_burn / 10000 PED.\n\n"
            "Current equipment and target are saved automatically. Named equipment setups and favorite mobs are independent."
        ), justify="left").pack(anchor="w")

    def create_sessions_tab(self):
        top = ttk.Frame(self.sessions_tab, padding=10)
        top.pack(fill="x")
        for row, actions in enumerate((
            (("Refresh", self.refresh_sessions_table), ("Load Current Skills from Selected Session", self.load_current_skills_from_selected_session), ("Add Selected to Mob Analysis", self.add_selected_sessions_to_analysis)),
            (("Remove Selected from Mob Analysis", self.remove_selected_sessions_from_analysis), ("Delete Selected Sessions", self.delete_selected_sessions), ("Clear Sessions", self.clear_sessions)),
        )):
            for column, (text, command) in enumerate(actions):
                ttk.Button(top, text=text, command=command).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=4)
        assignments = ttk.Frame(top)
        assignments.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(assignments, text="Change setup", command=lambda: self.open_session_assignment("setup")).pack(side="left")
        ttk.Button(assignments, text="Change mob", command=lambda: self.open_session_assignment("mob")).pack(side="left", padx=8)
        ttk.Label(top, text="Select sessions to change setup or mob. Double-click Weapon / Amp, Mob, PED cycled or Notes to edit.").grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        columns = (
            "started", "ended", "weapon", "mob", "notes", "attacks", "defended", "misses", "damage", "ped",
            "dpp", "effective_dpp", "loot", "loot_percent", "loot_events", "cost_per_kill", "skill_tt",
            "skill_tt_percent", "ped_h", "avg_skill_tt_per_hour", "skill_points",
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
            ("mob", "Mob", 170), ("notes", "Notes", 240), ("attacks", "Attacks", 75),
            ("defended", "J/E/D", 70), ("misses", "Misses", 70), ("damage", "Damage", 85),
            ("ped", "PED cycled", 95), ("dpp", "DPP", 70),
            ("effective_dpp", "Effective DPP", 110), ("loot", "Loot PED", 85),
            ("loot_percent", "Loot %", 75),
            ("loot_events", "Loot events", 90),
            ("cost_per_kill", "Cost/kill", 90),
            ("skill_tt", "Skill TT", 105),
            ("skill_tt_percent", "Skill TT %", 85),
            ("ped_h", "Base PED/h", 105),
            ("avg_skill_tt_per_hour", "Avg skill TT/h", 105),
            ("skill_points", "Point total", 115),
        ]
        for col, title, width in setup:
            self.sessions_tree.heading(col, text=title)
            self.sessions_tree.column(col, width=width, anchor="center" if col not in ("weapon", "mob", "notes") else "w")
        self.make_tree_sortable(self.sessions_tree, {col: title for col, title, _ in setup})
        self.sessions_pager = PagedTree(self, self.sessions_tree, self.sessions_tab)
        self.sessions_pager.controls.pack(fill="x", padx=10, pady=(0, 6))
        self.pack_table(self.sessions_tree, self.sessions_tab, padx=10, pady=(0, 10))
        self.sessions_tree.tag_configure("analysis_valid", background=self.ui_colors["valid_session"])
        self.sessions_tree.bind("<<TreeviewSelect>>", self.on_session_selected)
        self.sessions_tree.bind("<Double-1>", self.on_sessions_tree_double_click)

    def on_sessions_tree_double_click(self, event):
        """Edit metadata without changing the recorded combat/loot history."""
        tree = self.sessions_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        iid = tree.identify_row(event.y)
        column_token = tree.identify_column(event.x)
        if not iid or not column_token.startswith("#"):
            return
        try:
            column_index = int(column_token[1:]) - 1
            column_name = tree["columns"][column_index]
            session_index = int(str(iid).replace("session_", ""))
        except (ValueError, IndexError, tk.TclError):
            return
        if not (0 <= session_index < len(self.sessions)):
            return
        if column_name in ("weapon", "mob"):
            self.sessions_tree.selection_set(iid)
            self.open_session_assignment("setup" if column_name == "weapon" else "mob")
            return
        if column_name not in ("ped", "notes"):
            return

        bbox = tree.bbox(iid, column_name)
        if not bbox:
            return
        self.cancel_session_cell_edit()
        session = self.sessions[session_index]
        if column_name == "ped":
            initial_value = f"{parse_float(session.get('ped_cycled'), 0.0):.6f}"
        else:
            initial_value = str(session.get("notes", "") or "")

        x, y, width, height = bbox
        editor = ttk.Entry(tree)
        editor.insert(0, initial_value)
        editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        self.session_cell_editor = editor
        self.session_cell_editor_meta = (session_index, column_name, iid)
        editor.bind("<Return>", self.commit_session_cell_edit)
        editor.bind("<Escape>", self.cancel_session_cell_edit)
        editor.bind("<FocusOut>", self.commit_session_cell_edit)
        editor.focus_set()

    def commit_session_cell_edit(self, event=None):
        editor = self.session_cell_editor
        meta = self.session_cell_editor_meta
        if editor is None or not meta:
            return
        session_index, column_name, iid = meta
        raw_value = editor.get().strip()
        self.session_cell_editor = None
        self.session_cell_editor_meta = None
        try:
            editor.destroy()
        except tk.TclError:
            pass

        if not (0 <= session_index < len(self.sessions)):
            return
        session = self.sessions[session_index]
        if column_name == "ped":
            ped_cycled = parse_float(raw_value.replace(",", "."), None)
            if ped_cycled is None or not math.isfinite(ped_cycled) or ped_cycled < 0:
                messagebox.showerror("Invalid PED cycled", "PED cycled must be a number greater than or equal to 0.")
                return
            changes = {"ped_cycled": float(ped_cycled)}
            old_ped = parse_float(session.get("ped_cycled"), 0.0)
            if session.get("loot_events"):
                repriced = []
                attacks = parse_float(session.get("attacks_total"), 0.0)
                for row in session["loot_events"]:
                    fraction = (parse_float(row.get("cost_ped"), 0.0) / old_ped if old_ped > 0
                                else parse_float(row.get("manual_cost_fraction"), None))
                    if fraction is None and attacks > 0 and row.get("shots") is not None:
                        fraction = parse_float(row.get("shots"), 0.0) / attacks
                    # Retain the fraction when setting PED to zero, so a later
                    # correction can restore costs even in summary-only files.
                    repriced.append({**row, "cost_ped": fraction * ped_cycled, "manual_cost_fraction": fraction}
                                    if fraction is not None else dict(row))
                changes["loot_events"] = repriced
        elif column_name == "notes":
            changes = {"notes": raw_value}
        else:
            return

        staged = list(self.sessions)
        updated = {**session, **changes}
        staged[session_index] = updated
        if not self.save_session_updates(staged, loot_changed=False):
            return
        session.update(updated)
        if "loot_events" in changes:
            self.derived_cache().invalidate(session, keep_summary=True)

        self.refresh_sessions_table()
        if self.sessions_tree.exists(iid):
            self.sessions_tree.selection_set(iid)
            self.sessions_tree.focus(iid)
            self.sessions_tree.see(iid)
        self.refresh_visible_session_views()
        self.prune_session_cache()

    def cancel_session_cell_edit(self, event=None):
        editor = self.session_cell_editor
        self.session_cell_editor = None
        self.session_cell_editor_meta = None
        if editor is not None:
            try:
                editor.destroy()
            except tk.TclError:
                pass

    def open_session_assignment(self, kind):
        """Choose equipment or a favorite target for selected saved sessions."""
        if kind not in ("setup", "mob"):
            return
        indices = self.selected_session_indices_from_table()
        if not indices:
            messagebox.showwarning("No sessions selected", "Select one or more saved sessions first.")
            return
        # Capture objects, not row positions: a refresh cannot redirect edits.
        sessions = [self.sessions[index] for index in indices]
        if kind == "setup":
            choices = [(name, (name, row.get("weapon", "")))
                       for name, row in sorted(self.hunting_setups.items(), key=lambda pair: str(pair[0]).casefold())
                       if isinstance(row, dict)]
            columns = (("name", "Saved setup", 250), ("weapon", "Weapon", 280))
            hint = "Changes equipment and recalculates PED and loot-event costs from recorded attacks. Loot and skills stay unchanged."
        else:
            choices = [((row["mob"], row.get("maturity", "")), (row["mob"], row.get("maturity", "")))
                       for row in self.favorite_mobs if isinstance(row, dict) and row.get("mob")]
            choices.sort(key=lambda pair: tuple(str(value).casefold() for value in pair[0]))
            columns = (("mob", "Favorite mob", 330), ("maturity", "Maturity", 200))
            hint = "Changes mob and maturity, then updates HP-dependent metrics and mob analysis."
        if not choices:
            messagebox.showwarning("No saved choices", "Add saved setups or favorite mobs in Hunting Setup first.")
            return
        self.cancel_session_cell_edit()
        window = tk.Toplevel(self.root)
        window.title(f"Change {'setup' if kind == 'setup' else 'mob'} — {len(sessions)} selected session(s)")
        window.geometry("680x440")
        window.minsize(560, 360)
        window.transient(self.root)
        content = ttk.Frame(window, padding=12)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text=hint, wraplength=620).pack(fill="x", pady=(0, 10))
        search_var = tk.StringVar(window)
        search_row = ttk.Frame(content)
        search_row.pack(fill="x", pady=(0, 8))
        ttk.Label(search_row, text="Search:").pack(side="left", padx=(0, 8))
        search = ttk.Entry(search_row, textvariable=search_var)
        search.pack(side="left", fill="x", expand=True)
        footer = ttk.Frame(content)
        footer.pack(side="bottom", fill="x")
        tree = ttk.Treeview(content, columns=[column for column, _, _ in columns], show="headings", selectmode="browse", height=9)
        for column, title, width in columns:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor="w")
        self.pack_table(tree, content)
        preview_var = tk.StringVar(window, value="Choose a saved setup." if kind == "setup" else "Choose a favorite mob and maturity.")
        ttk.Label(footer, textvariable=preview_var, wraplength=620).pack(fill="x", pady=8)
        actions = ttk.Frame(footer)
        actions.pack(fill="x")

        def selected_choice():
            selected = tree.selection()
            return choices[int(selected[0])][0] if selected else None

        def preview(event=None):
            choice = selected_choice()
            save_button.configure(state="normal" if choice is not None else "disabled")
            if choice is None:
                preview_var.set("Choose a saved setup." if kind == "setup" else "Choose a favorite mob and maturity.")
            elif kind == "setup":
                row = self.hunting_setups.get(choice, {})
                attachments = ", ".join(str(name) for name in (row.get("attachments", []) or [])) or "None"
                preview_var.set(f"Amplifier: {row.get('amplifier') or 'None'} | Attachments: {attachments}")
            else:
                preview_var.set(f"Target: {choice[0]} / {choice[1]}")

        def filter_choices(*args):
            tree.delete(*tree.get_children())
            query = search_var.get().strip().casefold()
            for index, (_, values) in enumerate(choices):
                if query in " ".join(str(value) for value in values).casefold():
                    tree.insert("", "end", iid=str(index), values=values)
            preview()

        def save():
            choice = selected_choice()
            if choice is not None and self.assign_saved_session_choice(sessions, kind, choice):
                window.destroy()

        save_button = ttk.Button(actions, text="Save to selected sessions", command=save, state="disabled")
        save_button.pack(side="right")
        ttk.Button(actions, text="Cancel", command=window.destroy).pack(side="right", padx=8)
        tree.bind("<<TreeviewSelect>>", preview)
        search_var.trace_add("write", filter_choices)
        filter_choices()
        window.bind("<Escape>", lambda event: window.destroy())
        self.style_tracker_widgets(window)
        window.grab_set()
        search.focus_set()

    def assign_saved_session_choice(self, sessions, kind, choice):
        """Update equipment/target and turnover; preserve loot and skill data."""
        if kind == "setup":
            setup = self.hunting_setups.get(choice)
            if not isinstance(setup, dict):
                messagebox.showwarning("Setup unavailable", "The selected saved setup is no longer available.")
                return False
            changes = {"weapon": str(setup.get("weapon", "") or ""),
                       "amplifier": str(setup.get("amplifier", "") or ""),
                       "attachments": list(setup.get("attachments", []) or [])}
        elif kind == "mob":
            if not any(isinstance(row, dict) and (row.get("mob"), row.get("maturity", "")) == choice for row in self.favorite_mobs):
                messagebox.showwarning("Mob unavailable", "The selected favorite is no longer available.")
                return False
            changes = {"mob": choice[0], "maturity": choice[1]}
        else:
            return False
        indices = [index for index, row in enumerate(self.sessions) if any(row is session for session in sessions)]
        if not sessions or len(indices) != len(sessions):
            messagebox.showwarning("Sessions unavailable", "The selected sessions changed. Close this window and select them again.")
            return False
        # Stage copies so a failed primary write leaves live objects untouched.
        # Keep dictionaries: old sessions may contain fields unknown to us.
        staged = list(self.sessions)
        try:
            for index in indices:
                original = self.sessions[index]
                updated = {**original, **json.loads(json.dumps(changes))}
                # Equipment changes reprice costs. A target change needs only
                # new HP/grouping; retain compatibility for previously unpriced
                # sessions that still need their first turnover calculation.
                if kind == "setup" or not original.get("count_hunting"):
                    updated.update(self.recalculate_saved_session_turnover(original, updated))
                staged[index] = updated
        except ValueError as error:
            messagebox.showwarning("Cannot recalculate session", str(error))
            return False
        if not self.save_session_updates(staged, loot_changed=False):
            return False
        for index in indices:
            costs_changed = staged[index].get("loot_events") is not self.sessions[index].get("loot_events")
            self.sessions[index].update(staged[index])
            if costs_changed:
                self.derived_cache().invalidate(self.sessions[index], keep_summary=True)
        self.refresh_sessions_table()
        selected_iids = [f"session_{index}" for index in indices if self.sessions_tree.exists(f"session_{index}")]
        self.sessions_tree.selection_set(selected_iids)
        if selected_iids:
            self.sessions_tree.focus(selected_iids[0])
            self.sessions_tree.see(selected_iids[0])
        self.refresh_visible_session_views()
        self.prune_session_cache()
        return True

    def recalculate_saved_session_turnover(self, original, updated):
        """Reprice attacks and per-loot costs without replacing recorded loot."""
        weapon = updated.get("weapon", "")
        amplifier = updated.get("amplifier", "")
        attachments = updated.get("attachments", []) or []
        if (weapon not in WEAPONS or (amplifier and amplifier not in AMPLIFIERS)
                or any(name and name not in ATTACHMENTS for name in attachments[:3])):
            raise ValueError("The session equipment is missing from current item data. Choose a known saved setup first.")
        shot_cost = hunting_setup_cost_per_shot_ped(weapon, amplifier, attachments)
        old_cost = hunting_setup_cost_per_shot_ped(original.get("weapon", ""), original.get("amplifier", ""), original.get("attachments", []) or [])
        if original.get("weapon", "") not in WEAPONS:
            old_cost = 0.0
        if not math.isfinite(shot_cost) or shot_cost <= 0:
            raise ValueError("The selected equipment has no usable cost per shot. No sessions were changed.")
        attack_types = ("normal_hit", "crit", "defended_attack", "miss", "jammed")
        attacks = parse_float(original.get("attacks_total"), 0.0)
        if not attacks:
            attacks = sum(parse_float(original.get(key), 0.0) for key in ("normal_hits", "critical_hits", "missed_attacks"))
            attacks += parse_float(original.get("defended_attacks", original.get("jammed_attacks")), 0.0)
        old_ped = parse_float(original.get("ped_cycled"), 0.0)
        if not attacks and old_ped > 0 and old_cost > 0:
            # Very old files may have lost the attack summary or full log.
            attacks = old_ped / old_cost
        if not attacks and old_ped == 0:
            raw_attacks = sum(event.get("type") in attack_types for event in original.get("events", []) or [])
            attacks = raw_attacks or sum(parse_float(row.get("shots"), 0.0) for row in original.get("loot_events", []) or [])
        if not math.isfinite(attacks) or attacks < 0 or (attacks == 0 and old_ped > 0):
            raise ValueError("This old session has no usable attack count or original shot cost. No sessions were changed.")
        ped = attacks * shot_cost
        if not math.isfinite(ped):
            raise ValueError("This session has an invalid attack count. No sessions were changed.")
        result = {"ped_cycled": ped, "count_hunting": True}
        if original.get("loot_events"):
            # Modern sessions already retain exact per-receipt shots. Only scan
            # the raw history when an older receipt actually needs recovery.
            reconstructed = []
            if any(row.get("shots") is None for row in original["loot_events"]):
                raw_attacks = sum(event.get("type") in attack_types for event in original.get("events", []) or [])
                if raw_attacks == attacks:
                    reconstructed = self.reconstruct_loot_events_from_events({**original, "count_hunting": True})
            loot_events = []
            for index, original_row in enumerate(original["loot_events"]):
                row = dict(original_row)
                shots = parse_float(row.get("shots"), None)
                if shots is None and index < len(reconstructed):
                    rebuilt = reconstructed[index]
                    if (row.get("started_at") == rebuilt.get("started_at")
                            and math.isclose(parse_float(row.get("value_ped"), 0), rebuilt["value_ped"], abs_tol=1e-9)):
                        shots = rebuilt["shots"]
                if shots is not None and math.isfinite(shots) and shots >= 0:
                    row["cost_ped"] = shots * shot_cost
                elif attacks > 0 and not original.get("count_hunting") and not row.get("cost_ped"):
                    raise ValueError("This old session has no per-loot shot counts or costs. No sessions were changed.")
                elif old_cost > 0:
                    row["cost_ped"] = parse_float(row.get("cost_ped"), 0.0) * shot_cost / old_cost
                elif old_ped > 0:
                    row["cost_ped"] = parse_float(row.get("cost_ped"), 0.0) * ped / old_ped
                else:
                    raise ValueError("This old session has insufficient per-loot attack/cost data. No sessions were changed.")
                if not math.isfinite(row["cost_ped"]) or row["cost_ped"] < 0:
                    raise ValueError("This old session has an invalid loot-event cost. No sessions were changed.")
                loot_events.append(row)
            result["loot_events"] = loot_events
        return result

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
                self.analysis_sessions[existing_by_id[session_id]] = self.analysis_session_copy(
                    copied, self.analysis_sessions[existing_by_id[session_id]])
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
        self.refresh_visible_session_views()
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
        self.refresh_visible_session_views()

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

        fields = [("Efficiency (0-100):", self.analysis_efficiency_var)] + [
            (f"{looter_type} Looter (0-100):", self.analysis_looter_vars[looter_type])
            for looter_type in ("Animal", "Robot", "Mutant")
        ]
        for index, (label, variable) in enumerate(fields):
            row, pair = divmod(index, 2)
            ttk.Label(inputs, text=label).grid(row=row, column=pair * 2, sticky="w", pady=4)
            ttk.Entry(inputs, textvariable=variable, width=10).grid(row=row, column=pair * 2 + 1, sticky="w", padx=(8, 24), pady=4)
        actions = ttk.Frame(inputs)
        actions.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="Calculate", command=self.refresh_mob_analysis).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Use current skill levels", command=self.use_current_skill_looters).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Reload valid sessions", command=self.reload_analysis_sessions).pack(side="left")
        formula_hint = ttk.Label(
            inputs,
            text=(
                "Expected TT return = 86% + 7 × Efficiency / 100 + 7 × matching Looter / 100. "
                "Expected return after MU = expected TT return × historical loot MU multiplier."
            ),
            justify="left", wraplength=600,
        )
        formula_hint.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        formula_hint.bind("<Configure>", lambda event: formula_hint.configure(wraplength=max(1, event.width)))
        inputs.columnconfigure(3, weight=1)

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
        self.pack_table(self.mob_analysis_tree, self.mob_analysis_tab, padx=10, pady=(0, 10))
        self.mob_analysis_tree.bind("<Double-1>", self.open_mob_analysis_details)

    def reload_analysis_sessions(self):
        loaded = load_json(ANALYSIS_SESSIONS_FILE, [])
        self.analysis_sessions = loaded if isinstance(loaded, list) else []
        self.prune_session_cache()
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
            summary = self.session_loot_summary(session)
            item_totals = summary["item_values"]
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
            row["loot_events"] += summary["kills"]
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
                "loot_events": summary["kills"],
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

    def refresh_mob_analysis(self, *, persist_settings=True):
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
        if persist_settings:
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
        self.pack_table(item_tree, items_tab)
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
        def analysis_session_values(pair):
            _iid, row = pair
            return (
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
            )
        pager = PagedTree(self, session_tree, sessions_tab)
        pager.controls.pack(fill="x", padx=6, pady=6)
        self.pack_table(session_tree, sessions_tab)
        pager.set_rows([(f"analysis_session_{i}", row) for i, row in enumerate(result["session_rows"])], analysis_session_values)
        self.style_tracker_widgets(window)

    def create_session_details_tab(self):
        content = self.scrollable_tab_content(self.session_details_tab)
        top = ttk.Frame(content, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="Select a session in Previous Sessions to inspect exact skill gains, averages, hunting totals, and saved parsed events.",
        ).pack(anchor="w")

        projection = ttk.LabelFrame(content, text="Profession projection", padding=10)
        projection.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(projection, text="Profession:").grid(row=0, column=0, sticky="w")
        profession_combo = ttk.Combobox(
            projection,
            textvariable=self.session_projection_profession_var,
            values=sorted(PROFESSIONS.keys(), key=str.lower),
            state="readonly",
            width=45,
        )
        profession_combo.grid(row=0, column=1, sticky="ew", padx=8)
        profession_combo.bind("<<ComboboxSelected>>", self.refresh_selected_session_details)
        ttk.Label(projection, text="PED cycle:").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ped_entry = ttk.Entry(projection, textvariable=self.session_projection_ped_var, width=14)
        ped_entry.grid(row=0, column=3, sticky="w", padx=8)
        ped_entry.bind("<Return>", self.refresh_selected_session_details)
        ped_entry.bind("<FocusOut>", self.refresh_selected_session_details)
        ttk.Button(projection, text="Calculate", command=self.refresh_selected_session_details).grid(row=0, column=4, sticky="w", padx=4)
        projection.columnconfigure(1, weight=1)

        self.session_detail_summary_var = tk.StringVar(value="No session selected")
        summary = ttk.LabelFrame(content, text="Selected session summary", padding=10)
        summary.pack(fill="x", padx=10, pady=6)
        ttk.Label(summary, textvariable=self.session_detail_summary_var, justify="left").pack(anchor="w")

        projection_skill_frame = ttk.LabelFrame(
            content,
            text="Projected profession skill points",
            padding=6,
        )
        projection_skill_frame.pack(fill="both", expand=True, padx=10, pady=6)
        projection_columns = (
            "skill", "weight", "current", "session_tt", "projected_tt",
            "projected_gain", "projected_final", "hp_increase",
            "projected_hp", "profession_gain",
        )
        self.session_projection_skill_tree = ttk.Treeview(
            projection_skill_frame,
            columns=projection_columns,
            show="headings",
            height=9,
        )
        projection_setup = [
            ("skill", "Skill", 240),
            ("weight", "Weight %", 85),
            ("current", "Current points", 110),
            ("session_tt", "Session TT", 105),
            ("projected_tt", "Projected TT", 110),
            ("projected_gain", "Projected point gain", 135),
            ("projected_final", "Projected final points", 140),
            ("hp_increase", "Points / 1 HP", 105),
            ("projected_hp", "Projected HP gain", 120),
            ("profession_gain", "Profession gain", 120),
        ]
        for col, title, width in projection_setup:
            self.session_projection_skill_tree.heading(col, text=title)
            self.session_projection_skill_tree.column(
                col,
                width=width,
                anchor="w" if col == "skill" else "center",
            )
        self.make_tree_sortable(
            self.session_projection_skill_tree,
            {col: title for col, title, _ in projection_setup},
        )
        projection_y = ttk.Scrollbar(
            projection_skill_frame,
            orient="vertical",
            command=self.session_projection_skill_tree.yview,
        )
        projection_x = ttk.Scrollbar(
            projection_skill_frame,
            orient="horizontal",
            command=self.session_projection_skill_tree.xview,
        )
        self.session_projection_skill_tree.configure(
            yscrollcommand=projection_y.set,
            xscrollcommand=projection_x.set,
        )
        self.session_projection_skill_tree.grid(row=0, column=0, sticky="nsew")
        projection_y.grid(row=0, column=1, sticky="ns")
        projection_x.grid(row=1, column=0, sticky="ew")
        projection_skill_frame.rowconfigure(0, weight=1)
        projection_skill_frame.columnconfigure(0, weight=1)

        skill_frame = ttk.LabelFrame(content, text="Skill gains in selected session", padding=6)
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
        self.pack_table(self.session_detail_skill_tree, skill_frame)
        self.session_detail_skill_tree.bind(
            "<Double-1>",
            lambda event: self.open_skill_gain_details_from_tree(event, self.session_detail_skill_tree, "saved"),
        )

        events_frame = ttk.LabelFrame(content, text="Saved parsed events — all records available by page", padding=6)
        events_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.session_detail_events_text = tk.Text(events_frame, height=10, wrap="none")
        events_y = ttk.Scrollbar(events_frame, orient="vertical", command=self.session_detail_events_text.yview)
        events_x = ttk.Scrollbar(events_frame, orient="horizontal", command=self.session_detail_events_text.xview)
        self.session_detail_events_text.configure(yscrollcommand=events_y.set, xscrollcommand=events_x.set)
        self.session_detail_events_text.grid(row=0, column=0, sticky="nsew")
        events_y.grid(row=0, column=1, sticky="ns")
        events_x.grid(row=1, column=0, sticky="ew")
        events_frame.rowconfigure(0, weight=1)
        events_frame.columnconfigure(0, weight=1)
        self.detail_events = []
        self.detail_events_pager = PageControls(events_frame, self.render_session_event_page)
        self.detail_events_pager.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

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
        self.refresh_visible_session_views()

    def refresh_selected_session_details(self, event=None):
        if self.is_tab_active(getattr(self, "session_details_tab", None)):
            self.show_session_details(self.selected_session_from_table())
        self.update_session_summary()

    def show_session_details(self, session):
        reset_page = session is not self.detail_session
        self.detail_session = session
        self.detail_events = (session or {}).get("events") or []
        self.detail_events_pager.update_count(len(self.detail_events), reset=reset_page)
        self.session_detail_skill_tree.delete(*self.session_detail_skill_tree.get_children())
        self.session_projection_skill_tree.delete(*self.session_projection_skill_tree.get_children())
        self.render_session_event_page()
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
        loot_event_count = self.session_loot_summary(session)["kills"]
        cost_per_kill = ped_cycled / loot_event_count if loot_event_count else 0.0
        skill_tt_percent = percent(skill_tt_total, ped_cycled)
        skill_messages_per_attack = percent(skill_events_total, session.get('attacks_total', 0))
        session_hp_gain = sum(
            skill_hp_gain(skill_name, point_gain)
            for skill_name, point_gain in skill_points.items()
        )
        attachments = ", ".join(session.get("attachments", []) or []) or "-"
        profession_projection_text = self.profession_projection_text(session)
        projection_profession = self.selected_projection_profession()
        projection_ped_cycle = self.selected_projection_ped_cycle()
        projection_rows = []
        if projection_ped_cycle is not None:
            projection_rows, _projection_total = self.calculate_profession_projection_details(
                session,
                projection_profession,
                projection_ped_cycle,
            )
        for row in projection_rows:
            self.session_projection_skill_tree.insert(
                "",
                "end",
                values=(
                    row["skill"],
                    f'{row["weight"]:g}',
                    f'{row["current_points"]:.4f}',
                    f'{row["session_tt"]:.8f}',
                    f'{row["projected_tt"]:.8f}',
                    f'{row["projected_point_gain"]:.4f}',
                    f'{row["projected_final_points"]:.4f}',
                    f'{row["hp_increase"]:g}' if row["hp_increase"] > 0 else "-",
                    f'{row["projected_hp_gain"]:.6f}',
                    f'{row["profession_gain"]:.4f}',
                ),
            )
        self.apply_tree_sort(self.session_projection_skill_tree)
        saved_skill_snapshot_count = len(self.session_skill_snapshot(session))

        defended_attacks = int(session.get("defended_attacks", session.get("jammed_attacks", 0)) or 0)
        missed_attacks = int(session.get("missed_attacks", 0) or 0)

        self.session_detail_summary_var.set(
            f"Started: {session.get('started_at', '')} | Ended: {session.get('ended_at', '')}\n"
            f"Log: {session.get('chat_log_path', '')}\n"
            f"Saved current-skills snapshot: {saved_skill_snapshot_count} skills\n"
            f"Weapon: {session.get('weapon', '-') or '-'} | Amp: {session.get('amplifier', '') or '-'} | Attachments: {attachments}\n"
            f"Mob: {mob} | Count hunting: {bool(session.get('count_hunting', False))}\n"
            f"Notes: {str(session.get('notes', '') or '-')}\n"
            f"Attacks: {session.get('attacks_total', 0)} "
            f"(hits {session.get('normal_hits', 0)}, crits {session.get('critical_hits', 0)}, "
            f"defended {defended_attacks}, misses {missed_attacks}) | "
            f"Damage: {float(session.get('damage_total', 0.0)):.1f} | PED cycled: {ped_cycled:.4f} | "
            f"Loot: {loot_ped:.4f} PED ({loot_percent:.2f}%) | Loot events/kills: {loot_event_count} | "
            f"Cost/kill: {cost_per_kill:.6f} PED\n"
            f"Skill gain messages: {skill_events_total} | Point total: {skill_points_total:.4f} | "
            f"TT-equivalent total: {skill_tt_total:.4f} ({skill_tt_percent:.2f}% of cycled) | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}% | "
            f"Avg/message: {avg_points:.6f} points / {avg_tt:.6f} TT | "
            f"HP gained from skills: {session_hp_gain:.6f}\n"
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

    def render_session_event_page(self):
        text = self.session_detail_events_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        start = self.detail_events_pager.page * self.detail_events_pager.page_size
        lines = []
        for event in self.detail_events[start:start + self.detail_events_pager.page_size]:
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
            lines.append(line)
        if lines:
            text.insert("end", "\n".join(lines) + "\n")
        text.see("1.0")
        text.configure(state="disabled")

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
        self.cancel_profession_cell_edit()
        self.entries.clear()
        self.skill_tree.delete(*self.skill_tree.get_children())
        profession = PROFESSIONS[self.profession_var.get()]
        for skill_name, weight in sorted(profession["skills"].items(), key=lambda item: item[1], reverse=True):
            current = float(self.current_skills.get(skill_name, 0.0))
            draft = self.profession_skill_drafts.get(skill_name, {})
            data = {"weight": float(weight), "current": current, "delta": 0.0, **draft}
            self.entries[skill_name] = self.calculate_profession_skill_row(skill_name, data)
            self.skill_tree.insert("", "end", iid=skill_name)
            self.update_skill_tree_row(skill_name)
        self.update_profession_gain_total()

    def calculate_profession_skill_row(self, skill_name, data, column="delta", value=None):
        """Resolve any editable cell against the same forward/inverse TT curve."""
        result = dict(data)
        if value is not None:
            if not math.isfinite(value):
                raise ValueError("Enter a finite number.")
            result[column] = value
        current = result["current"]
        if not math.isfinite(current) or current < 0:
            raise ValueError("Current value must be a non-negative finite number.")
        factor = profession_weighted_value(skill_name, 1.0) * result["weight"] / 100.0
        if column in ("new", "skill_gain", "profession_gain"):
            if column == "skill_gain":
                new = current + result["skill_gain"]
            elif column == "profession_gain":
                if not factor:
                    raise ValueError("This skill has no contribution to the selected profession.")
                new = current + result["profession_gain"] / factor
            else:
                new = result["new"]
            # Unchanged legacy values above the verified curve remain usable.
            delta = 0.0 if new == current else skill_tt_value(new) - skill_tt_value(current)
        else:
            delta = result["delta"]
            new = current if delta == 0 else find_skill_after_tt_delta(current, delta)
            candidate = result.get("new")
            # Rounded TT samples can contain flat segments. Keep a directly
            # entered point value when it already has the target TT instead of
            # snapping it to the start of the segment on a later refresh.
            if candidate is not None and (candidate == current and delta == 0 or
                    0 <= candidate <= SKILL_TT_CURVE_MAX_POINTS and current <= SKILL_TT_CURVE_MAX_POINTS
                    and math.isclose(skill_tt_value(candidate), skill_tt_value(current) + delta, rel_tol=0, abs_tol=1e-12)):
                new = candidate
        gain = new - current
        result.update(delta=delta, new=new, skill_gain=gain, profession_gain=gain * factor)
        return result

    def update_profession_gain_total(self):
        total = sum(data["profession_gain"] for data in self.entries.values())
        self.total_gain_var.set(f"Total profession gain: {total:.4f}")

    def edit_profession_skill_value(self, skill_name, column, value):
        if column not in ("current", "delta", "new", "skill_gain", "profession_gain") or skill_name not in self.entries:
            raise ValueError("Choose an editable skill value.")
        data = self.calculate_profession_skill_row(skill_name, self.entries[skill_name], column, value)
        # Drafts stay separate from current_skills: live autosaves and closing
        # the app must never implicitly persist calculator previews.
        self.profession_skill_drafts[skill_name] = {key: data[key] for key in ("current", "delta", "new")}
        self.entries[skill_name] = data
        self.update_skill_tree_row(skill_name)
        self.update_profession_gain_total()

    def on_profession_skill_double_click(self, event):
        tree = self.skill_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        skill = tree.identify_row(event.y)
        token = tree.identify_column(event.x)
        if not skill or not token.startswith("#"):
            return
        column = tree.column(token, "id")
        if column not in ("current", "delta", "new", "skill_gain", "profession_gain"):
            return
        if not self.commit_profession_cell_edit():
            return
        bbox = tree.bbox(skill, column)
        if not bbox:
            return
        tree.selection_set(skill)
        editor = ttk.Entry(tree)
        editor.insert(0, repr(self.entries[skill][column]))
        editor.select_range(0, "end")
        x, y, width, height = bbox
        editor.place(x=x, y=y, width=width, height=height)
        self.profession_cell_editor = editor
        self.profession_cell_editor_meta = (skill, column)
        editor.bind("<Return>", self.commit_profession_cell_edit)
        editor.bind("<FocusOut>", self.commit_profession_cell_edit)
        editor.bind("<Escape>", self.cancel_profession_cell_edit)
        editor.focus_set()

    def commit_profession_cell_edit(self, event=None):
        editor = self.profession_cell_editor
        if editor is None:
            return True
        if self.profession_cell_committing:
            return False
        self.profession_cell_committing = True
        try:
            value = parse_float(editor.get().strip().replace(",", "."), None)
            if value is None:
                raise ValueError("Enter a number.")
            self.edit_profession_skill_value(*self.profession_cell_editor_meta, value)
        except ValueError as error:
            messagebox.showerror("Invalid skill value", str(error))
            editor.focus_set()
            return False
        finally:
            self.profession_cell_committing = False
        self.cancel_profession_cell_edit()
        return True

    def scroll_profession_table(self, view, *args):
        if self.commit_profession_cell_edit():
            view(*args)

    def position_profession_cell_editor(self, event=None):
        if self.profession_cell_editor is not None:
            bbox = self.skill_tree.bbox(*self.profession_cell_editor_meta)
            if bbox:
                x, y, width, height = bbox
                self.profession_cell_editor.place(x=x, y=y, width=width, height=height)

    def cancel_profession_cell_edit(self, event=None):
        editor = self.profession_cell_editor
        self.profession_cell_editor = None
        self.profession_cell_editor_meta = None
        if editor is not None:
            editor.destroy()

    def save_current_skills_from_table(self):
        if not self.commit_profession_cell_edit():
            return
        updated = dict(self.current_skills)
        updated.update({skill_name: float(data["new"]) for skill_name, data in self.entries.items()
                        if skill_name in self.profession_skill_drafts})
        try:
            save_current_skills(updated)
        except OSError as error:
            messagebox.showerror("Skills not saved", str(error))
            return
        self.current_skills = updated
        for skill in self.entries:
            self.profession_skill_drafts.pop(skill, None)
        self.load_profession_keep_selection()
        self.load_analysis_looters_from_current_skills()
        self.refresh_session_skill_tree()
        self.update_session_summary()
        messagebox.showinfo("Saved", f"New values saved as current skills to {CURRENT_SKILLS_FILE}")

    def reload_saved_skills(self):
        self.cancel_profession_cell_edit()
        self.profession_skill_drafts.clear()
        self.current_skills = load_current_skills()
        self.load_profession_keep_selection()
        self.load_analysis_looters_from_current_skills()
        self.refresh_session_skill_tree()
        messagebox.showinfo("Reloaded", f"Loaded skills from {CURRENT_SKILLS_FILE}")

    def calculate_profession_gain(self):
        for skill_name, data in self.entries.items():
            self.entries[skill_name] = self.calculate_profession_skill_row(skill_name, data)
            self.update_skill_tree_row(skill_name)
        self.update_profession_gain_total()

    def update_skill_tree_row(self, skill_name):
        if skill_name not in self.entries or not self.skill_tree.exists(skill_name):
            return
        data = self.entries[skill_name]
        self.skill_tree.item(skill_name, values=(skill_name, f"{data['weight']:g}", f"{data['current']:.4f}", f"{data['delta']:.8f}", f"{data['new']:.4f}", f"{data['skill_gain']:.4f}", f"{data['profession_gain']:.4f}"))

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
        tree = self.hunting_setups_tree
        tree.delete(*tree.get_children())
        self.hunting_setup_iid_to_name = {}
        for index, name in enumerate(sorted(self.hunting_setups, key=str.lower)):
            setup = self.hunting_setups[name]
            weapon = setup.get("weapon", "") if isinstance(setup, dict) else "Invalid setup"
            iid = f"setup_{index}"
            self.hunting_setup_iid_to_name[iid] = name
            tree.insert("", "end", iid=iid, values=(name, weapon))
            if name == self.hunting_setup_name_var.get():
                tree.selection_set(iid)
                tree.focus(iid)

    def quick_list_selection(self, tree, event=None):
        if event is not None and getattr(event, "num", None) == 1:
            # Clicking headings or blank space must not apply the old selection.
            iid = tree.identify_row(event.y)
        else:
            selected = tree.selection()
            iid = selected[0] if selected else ""
        if iid:
            tree.selection_set(iid)
            tree.focus(iid)
        return iid

    def use_selected_hunting_setup(self, event=None):
        iid = self.quick_list_selection(self.hunting_setups_tree, event)
        name = self.hunting_setup_iid_to_name.get(iid)
        if name is not None:
            self.hunting_setup_name_var.set(name)
            self.load_named_hunting_setup()

    def refresh_favorite_mobs(self):
        tree = self.favorite_mobs_tree
        tree.delete(*tree.get_children())
        self.favorite_mob_iid_to_index = {}
        rows = [(index, row) for index, row in enumerate(self.favorite_mobs) if isinstance(row, dict) and row.get("mob")]
        for index, row in sorted(rows, key=lambda pair: (str(pair[1]["mob"]).casefold(), str(pair[1].get("maturity", "")).casefold())):
            iid = f"favorite_{index}"
            self.favorite_mob_iid_to_index[iid] = index
            tree.insert("", "end", iid=iid, values=(row["mob"], row.get("maturity", "")))
            if row["mob"] == self.mob_var.get() and row.get("maturity", "") == self.maturity_var.get():
                tree.selection_set(iid)
                tree.focus(iid)

    def add_current_favorite_mob(self):
        mob, maturity = self.mob_var.get(), self.maturity_var.get()
        if mob not in MOBS or maturity not in (MOBS[mob].get("maturities") or {}):
            messagebox.showwarning("Choose a target", "Choose a known mob and maturity first.")
            return
        if not any(isinstance(row, dict) and row.get("mob") == mob and row.get("maturity", "") == maturity for row in self.favorite_mobs):
            self.favorite_mobs.append({"mob": mob, "maturity": maturity})
            save_json(FAVORITE_MOBS_FILE, self.favorite_mobs)
        self.refresh_favorite_mobs()
        self.favorite_mob_status_var.set(f"Favorite: {mob} / {maturity}")

    def use_selected_favorite_mob(self, event=None):
        iid = self.quick_list_selection(self.favorite_mobs_tree, event)
        index = self.favorite_mob_iid_to_index.get(iid)
        if index is None:
            return
        row = self.favorite_mobs[index]
        mob, maturity = row["mob"], row.get("maturity", "")
        if mob not in MOBS:
            messagebox.showwarning("Mob unavailable", f"'{mob}' is not in the current mob data. The favorite has been kept.")
            return
        self.clear_mob_filter()
        self.mob_var.set(mob)
        self.maturity_var.set(maturity)
        self.update_maturity_values()
        self.on_hunting_changed()
        self.favorite_mob_status_var.set(f"Target: {mob} / {self.maturity_var.get()}")

    def remove_selected_favorite_mob(self):
        iid = self.quick_list_selection(self.favorite_mobs_tree)
        index = self.favorite_mob_iid_to_index.get(iid)
        if index is None:
            return
        row = self.favorite_mobs[index]
        if not messagebox.askyesno("Remove favorite", f"Remove '{row['mob']} / {row.get('maturity', '')}' from favorites?"):
            return
        del self.favorite_mobs[index]
        save_json(FAVORITE_MOBS_FILE, self.favorite_mobs)
        self.refresh_favorite_mobs()
        self.favorite_mob_status_var.set("Favorite removed. Current target stays unchanged.")

    def current_hunting_setup_payload(self):
        return {
            "weapon": self.weapon_var.get(),
            "amplifier": self.selected_amplifier(),
            "attachments": self.selected_attachments(),
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
            f"Replace the saved equipment in '{existing_name}'? The selected mob and favorites stay unchanged.",
        ):
            return

        # Preserve extension fields in older setups. Their target was imported
        # into favorites before it is removed from an explicitly updated setup.
        previous = self.hunting_setups.get(save_name)
        payload = dict(previous) if isinstance(previous, dict) else {}
        payload.update(self.current_hunting_setup_payload())
        payload.pop("mob", None)
        payload.pop("maturity", None)
        self.hunting_setups[save_name] = payload
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

        self.clear_weapon_filter()
        self.clear_amplifier_filter()
        self.clear_attachment_filter()

        self.weapon_var.set(weapon if weapon in WEAPONS else "")
        self.amplifier_var.set(amplifier if amplifier in AMPLIFIERS else "")
        while len(attachments) < 3:
            attachments.append("")
        for attachment_var, attachment_name in zip(self.attachment_vars, attachments):
            attachment_var.set(attachment_name if attachment_name in ATTACHMENTS else "")

        self.count_hunting_var.set(bool(setup.get("count_hunting", False)))
        self.hunting_setup_name_var.set(setup_name)
        self.refresh_hunting_info()
        self.hunting_setup_status_var.set(f"Loaded setup: {setup_name}")
        self.save_state()

    def delete_named_hunting_setup(self):
        requested_name = self.hunting_setup_name_var.get().strip()
        selected = self.hunting_setups_tree.selection()
        if selected:
            requested_name = self.hunting_setup_iid_to_name.get(selected[0], requested_name)
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
        if hasattr(self, "favorite_mobs_tree"):
            # Manual target edits should not leave an unrelated favorite
            # highlighted. This changes selection without applying a target.
            self.favorite_mobs_tree.selection_remove(*self.favorite_mobs_tree.selection())
            for iid, index in self.favorite_mob_iid_to_index.items():
                row = self.favorite_mobs[index]
                if row["mob"] == self.mob_var.get() and row.get("maturity", "") == self.maturity_var.get():
                    self.favorite_mobs_tree.selection_set(iid)
                    break
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
        self.next_log_poll_at = 0.0
        self.last_log_fingerprint_check_at = 0.0

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
        self.update_session_summary()
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
        self.next_log_poll_at = 0.0
        if hasattr(self, "pause_sync_button"):
            self.pause_sync_button.configure(text="Pause Sync")
        self.monitor_status_var.set("Syncing")
        self.monitor_progress_var.set(f"Resuming from offset {self.log_offset:,}...")
        self.append_event(f"Resumed sync from offset {self.log_offset}")
        self.start_log_reader_until_current_eof()

    def monitor_tick(self):
        """Drive live sync without allowing one callback error to kill it.

        Tkinter does not automatically repeat an ``after`` callback when that
        callback raises. A single unexpected parsing/UI exception could
        therefore stop the live monitor forever while the window still showed
        "Syncing". Always schedule the next tick in ``finally`` and let the
        reader recover automatically.
        """
        try:
            if self.monitoring:
                self.process_reader_queue(max_batches=8, time_budget_ms=45)
                self.refresh_live_ui()
                self.maybe_persist_live_state()

                if self.sync_paused:
                    if not self.reader_active:
                        self.monitor_status_var.set("Paused")
                elif not self.reader_active and not self.reader_done_pending:
                    now = time.monotonic()
                    if now >= self.next_log_poll_at:
                        self.next_log_poll_at = now + self.log_poll_interval
                        self.start_log_reader_until_current_eof()

                # Self-heal a worker that exited without its terminal queue
                # message being applied for any reason. Normally the queued
                # 'done'/'error' message is handled above; this is only a safety
                # net so the monitor cannot remain permanently stuck active.
                if (
                    self.reader_active
                    and self.reader_thread is not None
                    and not self.reader_thread.is_alive()
                    and self.reader_queue.empty()
                ):
                    self.reader_active = False
                    self.reader_done_pending = False
                    self.monitor_status_var.set("Syncing")
                    self.monitor_progress_var.set("Reader recovered; waiting for new log lines...")
        except Exception as ex:
            self.monitor_tick_errors += 1
            self.monitor_status_var.set("Sync recovered after error")
            self.monitor_progress_var.set(f"Monitor error recovered: {ex}")
            self.append_event(f"Monitor tick error recovered: {type(ex).__name__}: {ex}")
            # If the worker is already gone, clear stale lifecycle flags so the
            # next tick can start a fresh reader. Do not clear reader_active for
            # a worker that is genuinely still running.
            if self.reader_thread is None or not self.reader_thread.is_alive():
                self.reader_active = False
                self.reader_done_pending = False
        finally:
            try:
                self.root.after(100, self.monitor_tick)
            except tk.TclError:
                pass

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

        # Detect clear/replace before launching the worker. A size shrink is
        # cheap to detect and is checked on every poll. Fingerprint validation
        # is more expensive, so only do it periodically and only when the saved
        # fingerprint belongs to the exact offset we are validating.
        if current_size < self.log_offset:
            cutoff_text = self.state.get("last_log_read_at", "")
            self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
            self.append_event(f"chat.log became smaller than saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
            self.log_offset = 0
            self.last_log_fingerprint_check_at = time.monotonic()
        elif self.log_offset > 0:
            now = time.monotonic()
            saved_offset = int(self.state.get("last_log_offset", -1) or -1)
            should_validate = (
                saved_offset == int(self.log_offset)
                and now - self.last_log_fingerprint_check_at >= self.log_fingerprint_check_interval
            )
            if should_validate:
                self.last_log_fingerprint_check_at = now
                saved_fingerprint = self.state.get("last_log_fingerprint", "")
                current_fingerprint = log_resume_fingerprint(path, self.log_offset)
                if saved_fingerprint and current_fingerprint and saved_fingerprint != current_fingerprint:
                    cutoff_text = self.state.get("last_log_read_at", "")
                    self.log_time_cutoff_at = parse_iso_datetime(cutoff_text)
                    self.append_event(f"chat.log content changed before saved offset; restarting from beginning and skipping lines before {cutoff_text or 'previous read time'}")
                    self.log_offset = 0

        if current_size <= self.log_offset:
            # Do NOT call save_state() here. This path runs while simply waiting
            # for new chat lines; saving every poll caused repeated fsync/hash
            # work on Tkinter's thread and was a major source of sync stalls.
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

                        if len(batch) >= 100:
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
        """Apply worker messages in small UI-friendly slices.

        A worker always puts ``done`` after all of its batch/progress messages.
        Because Queue is FIFO, seeing ``done`` already guarantees that all
        earlier messages from that worker were consumed. Older code used
        ``qsize()`` to decide whether done could be finalized; qsize is only an
        approximation and could leave ``reader_done_pending`` stuck forever,
        preventing the next reader from starting.
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
                    f"offset {current_offset:,}/{end_offset:,}"
                )
                continue

            if kind == "batch":
                _, events, skipped_by_time, newest_iso, batch_end_offset = item
                if skipped_by_time:
                    self.append_event(f"Skipped {skipped_by_time} old parsed events before last_log_read_at")

                apply_errors = 0
                for event in events:
                    try:
                        self.apply_event(event)
                    except Exception as ex:
                        apply_errors += 1
                        self.append_event(
                            f"Skipped one parsed event after {type(ex).__name__}: {ex} | "
                            f"{event.get('timestamp', '')} {event.get('message', event)}"
                        )

                # Advance to the batch boundary even if one malformed event was
                # skipped. Otherwise the same broken line would be replayed on
                # every restart and could repeatedly wedge live sync.
                self.log_offset = int(batch_end_offset)
                if self.current_session is not None:
                    self.current_session.end_offset = self.log_offset
                if newest_iso:
                    self.pending_last_log_read_at = str(newest_iso)
                processed_batches += 1
                self.reader_processed_batches += 1
                refresh_needed = True
                self.live_ui_dirty = True
                error_text = f" | skipped errors: {apply_errors}" if apply_errors else ""
                self.monitor_progress_var.set(
                    f"Applied {len(events):,} parsed events | offset {self.log_offset:,}" + error_text
                )
                continue

            if kind == "done":
                final_offset = int(item[1])
                final_last_read_at = str(item[2] if len(item) > 2 else "" or "")
                self.reader_active = False
                self.reader_done_pending = False
                self.reader_final_offset = final_offset
                self.reader_final_last_read_at = final_last_read_at
                self.log_offset = final_offset
                if self.current_session is not None:
                    self.current_session.end_offset = self.log_offset
                if final_last_read_at:
                    self.pending_last_log_read_at = final_last_read_at
                if self.log_time_cutoff_at is not None:
                    self.log_time_cutoff_at = None
                    if self.current_session is not None:
                        self.current_session.log_cutoff_at = ""

                # Do not force disk writes or a full UI rebuild for every tiny
                # live-log read. Hunting can produce several reader completions
                # per second. Normal throttles below persist/refresh frequently
                # enough without freezing Tkinter.
                refresh_needed = True
                self.live_ui_dirty = True
                self.monitor_status_var.set("Syncing")
                self.monitor_progress_var.set(
                    f"Caught up. Current offset: {self.log_offset:,}. Waiting for new lines..."
                )
                self.next_log_poll_at = min(
                    self.next_log_poll_at or time.monotonic(),
                    time.monotonic() + self.log_poll_interval,
                )
                continue

            if kind == "error":
                _, message = item
                self.reader_active = False
                self.reader_done_pending = False
                self.reader_error = str(message)
                self.monitor_status_var.set("Syncing")
                self.monitor_progress_var.set(f"Reader error recovered: {message}. Retrying...")
                self.append_event(f"Reader error recovered: {message}")
                self.live_ui_dirty = True
                self.next_log_poll_at = time.monotonic() + 0.5
                continue

        if refresh_needed or force_refresh:
            self.refresh_live_ui(force=force_refresh)
            self.maybe_persist_live_state(force=force_refresh)

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
        self.maybe_persist_live_state()
        self.refresh_live_ui()

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
            setattr(
                session,
                "_shots_since_loot_event",
                int(getattr(session, "_shots_since_loot_event", 0) or 0) + 1,
            )
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
        """Add one loot chat line to the correct kill/loot event.

        Grouping rule:
        - loot lines with the same chat.log second belong to the same event;
        - any player attack between loot lines forces a new event, even when both
          loot lines have the same second;
        - loot lines from different seconds are always separate events.

        Event cost is exactly the number of attack messages seen since the
        previous loot event multiplied by the selected setup's cost per shot.
        """
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

        shots_since_loot = int(getattr(session, "_shots_since_loot_event", 0) or 0)
        timestamp = str(event.get("timestamp", "") or "")
        continue_previous = bool(
            loot_events
            and shots_since_loot == 0
            and timestamp
            and not loot_events[-1].get("excluded_from_loot")
            and item not in (loot_events[-1].get("excluded_loot_items") or [])
            and timestamp == str(loot_events[-1].get("ended_at", "") or "")
        )

        if continue_previous:
            loot_event = loot_events[-1]
        else:
            cost_per_shot = hunting_setup_cost_per_shot_ped(
                session.weapon,
                session.amplifier,
                session.attachments,
            )
            event_cost = shots_since_loot * cost_per_shot if session.count_hunting else 0.0
            loot_event = {
                "index": len(loot_events) + 1,
                "started_at": timestamp,
                "ended_at": timestamp,
                "value_ped": 0.0,
                "cost_ped": event_cost,
                "shots": shots_since_loot,
                "items": {},
                "messages": [],
            }
            loot_events.append(loot_event)
            # The first loot line closes the current kill. Extra item lines in
            # the same second keep this at zero and are merged into this event.
            setattr(session, "_shots_since_loot_event", 0)

        loot_event["ended_at"] = timestamp or loot_event.get("ended_at", "")
        loot_event["value_ped"] = float(loot_event.get("value_ped", 0.0) or 0.0) + loot_value
        loot_event.setdefault("items", {})[item] = int(loot_event.setdefault("items", {}).get(item, 0) or 0) + quantity
        loot_event.setdefault("messages", []).append(event.get("message", ""))
        if getattr(session, "_has_loot_exclusions", False):
            session.loot_event_count += int(not continue_previous)
        else:
            session.loot_event_count = len(loot_events)
        self.derived_cache().invalidate(vars(session))

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

    def calculate_profession_projection_details(self, session, profession_name: str, ped_cycle: float | None = None):
        """Return per-skill projection rows and the total profession gain."""
        profession = PROFESSIONS.get(profession_name)
        if not profession:
            return [], 0.0
        if isinstance(session, MonitorSession):
            session = vars(session)  # Read-only: avoid copying the entire event history.

        skill_tt = session.get("skill_gains_tt", {}) or {}
        ped_cycled = float(session.get("ped_cycled", 0.0) or 0.0)
        rows = []
        total = 0.0

        for skill, weight in profession["skills"].items():
            session_tt = float(skill_tt.get(skill, 0.0) or 0.0)
            if ped_cycle is None:
                projected_tt = session_tt
            elif ped_cycled <= 0:
                projected_tt = 0.0
            else:
                projected_tt = session_tt / ped_cycled * float(ped_cycle)

            current_points = float(self.current_skills.get(skill, 0.0) or 0.0)
            try:
                projected_final = find_skill_after_tt_delta(current_points, projected_tt)
                point_gain = projected_final - current_points
            except Exception:
                projected_final = current_points
                point_gain = 0.0

            profession_gain = (
                profession_weighted_value(skill, point_gain)
                * float(weight)
                / 100.0
            )
            hp_increase = parse_float(SKILL_HP_INCREASES.get(skill), 0.0)
            projected_hp_gain = skill_hp_gain(skill, point_gain)
            total += profession_gain
            rows.append({
                "skill": skill,
                "weight": float(weight),
                "current_points": current_points,
                "session_tt": session_tt,
                "projected_tt": projected_tt,
                "projected_point_gain": point_gain,
                "projected_final_points": projected_final,
                "hp_increase": hp_increase,
                "projected_hp_gain": projected_hp_gain,
                "profession_gain": profession_gain,
            })

        rows.sort(key=lambda row: (-row["weight"], row["skill"].casefold()))
        return rows, total

    def calculate_session_projected_hp_details(self, session, ped_cycle: float | None = None):
        """Project HP gain from every HP-granting skill gained in the session."""
        if isinstance(session, MonitorSession):
            session = vars(session)

        skill_tt = session.get("skill_gains_tt", {}) or {}
        skill_points = session.get("skill_gains_points", {}) or {}
        ped_cycled = float(session.get("ped_cycled", 0.0) or 0.0)
        skill_names = set(skill_tt) | set(skill_points)
        rows = []

        for skill in skill_names:
            hp_increase = parse_float(SKILL_HP_INCREASES.get(skill), 0.0)
            if hp_increase <= 0:
                continue

            session_tt = float(skill_tt.get(skill, 0.0) or 0.0)
            if ped_cycle is None:
                projected_tt = session_tt
            elif ped_cycled <= 0:
                projected_tt = 0.0
            else:
                projected_tt = session_tt / ped_cycled * float(ped_cycle)

            current_points = float(self.current_skills.get(skill, 0.0) or 0.0)
            try:
                projected_final = find_skill_after_tt_delta(current_points, projected_tt)
                projected_point_gain = max(0.0, projected_final - current_points)
            except Exception:
                projected_final = current_points
                projected_point_gain = 0.0

            rows.append({
                "skill": skill,
                "current_points": current_points,
                "session_tt": session_tt,
                "projected_tt": projected_tt,
                "projected_point_gain": projected_point_gain,
                "projected_final_points": projected_final,
                "hp_increase": hp_increase,
                "projected_hp_gain": projected_point_gain / hp_increase,
            })

        rows.sort(key=lambda row: (-row["projected_hp_gain"], row["skill"].casefold()))
        return rows, sum(row["projected_hp_gain"] for row in rows)

    def calculate_profession_projection(self, session, profession_name: str, ped_cycle: float | None = None) -> float:
        _rows, total = self.calculate_profession_projection_details(
            session,
            profession_name,
            ped_cycle,
        )
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
        if ped_cycle is None or not math.isfinite(ped_cycle) or ped_cycle < 0:
            return None
        return ped_cycle

    def format_ped_cycle(self, ped_cycle: float) -> str:
        return f"{ped_cycle:,.2f}".rstrip("0").rstrip(".")

    def profession_projection_text(self, session) -> str:
        profession_name = self.selected_projection_profession()
        ped_cycle = self.selected_projection_ped_cycle()
        if ped_cycle is None:
            return f"{profession_name} projected gain: enter a finite, non-negative PED amount."
        session_data = vars(session) if isinstance(session, MonitorSession) else session
        if float(session_data.get("ped_cycled", 0.0) or 0.0) <= 0:
            return "Projection unavailable until the session has a positive PED cycled total."

        _rows, projected = self.calculate_profession_projection_details(
            session,
            profession_name,
            ped_cycle,
        )
        _all_hp_rows, projected_hp = self.calculate_session_projected_hp_details(
            session,
            ped_cycle,
        )
        return (
            f"{profession_name} projected gain at {self.format_ped_cycle(ped_cycle)} PED: "
            f"{projected:.4f} | Projected HP gain (all skills): {projected_hp:.6f}"
        )

    def calculate_session_combat_metrics(self, session):
        """Derived display values only; never add fields to saved sessions.

        Both DPP values use damage/PEC. Effective DPP uses the saved session's
        mob and maturity, and the same average cost/kill used by session details
        (total PED cycled / loot events). Loot events are a proxy for kills.
        """
        if isinstance(session, MonitorSession):
            data = vars(session)
            kills = session.loot_event_count if getattr(session, "_has_loot_exclusions", False) else len(session.loot_events)
        else:
            data = session or {}
            kills = self.session_loot_summary(data)["kills"]
            if not kills and not data.get("loot_events") and not data.get("events"):
                kills = max(0, int(data.get("loot_event_count", 0) or 0))
        ped = parse_float(data.get("ped_cycled"), 0.0)
        damage = parse_float(data.get("damage_total"), 0.0)
        cost = ped / kills if kills and math.isfinite(ped) and ped > 0 else None
        dpp = damage / ped / 100.0 if math.isfinite(damage) and math.isfinite(ped) and ped > 0 else None
        mob = MOBS.get(data.get("mob", "")) or {}
        maturity = (mob.get("maturities") or {}).get(data.get("maturity", "")) or {}
        hp = parse_float(maturity.get("hp"), None)
        effective_dpp = hp / cost / 100.0 if cost and hp is not None and math.isfinite(hp) and hp > 0 else None
        return {"kills": kills, "cost_per_kill": cost, "dpp": dpp, "effective_dpp": effective_dpp}

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
            for variable in self.monitor_metric_vars.values():
                variable.set("—")
            self.monitor_combat_var.set("")
            self.monitor_skills_var.set("")
            self.monitor_projection_var.set("Start a session to project profession and HP gains.")
            return
        s = self.current_session
        metrics = self.calculate_session_combat_metrics(s)
        total_skill_gain_events = sum(s.skill_gain_events_by_skill.values())
        loot_percent = percent(s.loot_ped_total, s.ped_cycled)
        skill_tt_percent = percent(s.skill_gain_tt_total, s.ped_cycled)
        skill_messages_per_attack = percent(total_skill_gain_events, s.attacks_total)
        session_hp_gain = sum(
            skill_hp_gain(skill_name, point_gain)
            for skill_name, point_gain in s.skill_gains_points.items()
        )
        attachments = ", ".join(s.attachments or []) or "-"
        self.session_summary_var.set(
            f"Started: {s.started_at} | Weapon: {s.weapon or '-'} | Amp: {s.amplifier or '-'} | Attachments: {attachments}\n"
            f"Mob: {s.mob or '-'} {s.maturity or ''}"
        )
        display_values = {
            "ped": f"{s.ped_cycled:.4f}",
            "loot": f"{s.loot_ped_total:.4f} / {loot_percent:.2f}%" if s.ped_cycled > 0 else f"{s.loot_ped_total:.4f} / —",
            "damage": f"{s.damage_total:.1f}",
            "kills": str(metrics["kills"]),
            "cost": f'{metrics["cost_per_kill"]:.6f}' if metrics["cost_per_kill"] is not None else "—",
            "dpp": f'{metrics["dpp"]:.3f}' if metrics["dpp"] is not None else "—",
            "effective_dpp": f'{metrics["effective_dpp"]:.3f}' if metrics["effective_dpp"] is not None else "—",
            "skill_tt": f"{s.skill_gain_tt_total:.4f} / {skill_tt_percent:.2f}%" if s.ped_cycled > 0 else f"{s.skill_gain_tt_total:.4f} / —",
        }
        for key, value in display_values.items():
            self.monitor_metric_vars[key].set(value)
        self.monitor_combat_var.set(
            f"Attacks: {s.attacks_total} (hits {s.normal_hits}, crits {s.critical_hits}, "
            f"defended {s.defended_attacks}, misses {s.missed_attacks})"
        )
        self.monitor_skills_var.set(
            f"Skill gains: {total_skill_gain_events} messages | Point total: {s.skill_gain_points_total:.4f} | "
            f"Skill messages/attack: {skill_messages_per_attack:.2f}% | "
            f"HP gained from skills: {session_hp_gain:.6f}"
        )
        self.monitor_projection_var.set(self.profession_projection_text(s))

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
        changed = False
        for skill_name, data in self.entries.items():
            if skill_name in self.profession_skill_drafts or (self.profession_cell_editor_meta and self.profession_cell_editor_meta[0] == skill_name):
                continue
            current = float(self.current_skills.get(skill_name, 0.0))
            if current == float(data.get("current", 0.0)):
                continue
            data["current"] = current
            self.entries[skill_name] = self.calculate_profession_skill_row(skill_name, data)
            self.update_skill_tree_row(skill_name)
            changed = True
        if changed:
            self.update_profession_gain_total()

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
                self.refresh_loot_tab(force=False)
                self.last_loot_live_refresh_at = now

        self.flush_event_text()
        self.live_ui_dirty = False
        self.live_ui_last_refresh_at = now

    def load_profession_keep_selection(self):
        selected_profession = self.profession_var.get()
        selected = self.skill_tree.selection()
        selected_skill = selected[0] if selected else None
        self.load_profession()
        self.profession_var.set(selected_profession)
        if selected_skill and self.skill_tree.exists(selected_skill):
            self.skill_tree.selection_set(selected_skill)
            self.skill_tree.focus(selected_skill)

    def refresh_sessions_table(self):
        self.prune_session_cache()
        valid_analysis_ids = {
            str(session.get("id", "") or "")
            for session in self.analysis_sessions
            if isinstance(session, dict) and session.get("id")
        }
        rows = [(f"session_{index}", self.sessions[index])
                for index in range(len(self.sessions) - 1, -1, -1)]
        def tag_rows(visible):
            for iid, session in visible:
                tags = ("analysis_valid",) if str(session.get("id", "") or "") in valid_analysis_ids else ()
                self.sessions_tree.item(iid, tags=tags)
        self.sessions_pager.set_rows(rows, self.session_table_values, on_render=tag_rows)

    def session_table_values(self, pair):
        _iid, session = pair
        skills = session.get("skill_gains_points", {}) or {}
        skill_tt = session.get("skill_gains_tt", {}) or {}

        # Backward-compatible totals for sessions saved by older versions.
        skill_tt_total = float(session.get("skill_gain_tt_total", sum(float(v) for v in skill_tt.values())))
        skill_points_total = float(session.get("skill_gain_points_total", sum(float(v) for v in skills.values())))

        mob = f"{session.get('mob', '')} {session.get('maturity', '')}".strip()
        notes = str(session.get("notes", "") or "").replace("\r", " ").replace("\n", " ")
        damage_total = float(session.get('damage_total', 0.0) or 0.0)
        ped_cycled = float(session.get('ped_cycled', 0.0))
        loot_ped = float(session.get('loot_ped_total', 0.0))
        combat = self.calculate_session_combat_metrics(session)
        loot_event_count = combat["kills"]
        cost_per_kill = combat["cost_per_kill"] or 0.0
        weapon = session.get("weapon", "")
        amplifier = session.get("amplifier", "")
        attachments = session.get("attachments", []) or []
        weapon_display = weapon
        if amplifier:
            weapon_display = f"{weapon or '-'} + {amplifier}"
        has_weapon_stats = weapon in WEAPONS
        # Session DPP is based on what actually happened in the session:
        # damage per PEC = total damage / (PED cycled * 100 PEC/PED).
        dpp = damage_total / ped_cycled / 100.0 if ped_cycled > 0 else 0.0
        ped_per_hour = hunting_setup_ped_per_hour(weapon, amplifier, attachments) if has_weapon_stats else 0.0
        skill_tt_percent = percent(skill_tt_total, ped_cycled)
        avg_skill_tt_per_hour = (skill_tt_percent / 100.0) * ped_per_hour if ped_per_hour else 0.0
        defended_attacks = int(session.get("defended_attacks", session.get("jammed_attacks", 0)) or 0)
        missed_attacks = int(session.get("missed_attacks", 0) or 0)
        return (
            session.get("started_at", ""),
            session.get("ended_at", ""),
            weapon_display,
            mob,
            notes,
            session.get("attacks_total", 0),
            defended_attacks,
            missed_attacks,
            f"{damage_total:.1f}",
            f"{ped_cycled:.4f}",
            f"{dpp:.3f}" if dpp else "",
            f'{combat["effective_dpp"]:.3f}' if combat["effective_dpp"] is not None else "—",
            f"{loot_ped:.4f}",
            f"{percent(loot_ped, ped_cycled):.2f}%",
            loot_event_count,
            f"{cost_per_kill:.6f}" if cost_per_kill else "",
            f"{skill_tt_total:.4f}",
            f"{skill_tt_percent:.2f}%",
            f"{ped_per_hour:.2f}" if ped_per_hour else "",
            f"{avg_skill_tt_per_hour:.4f}" if avg_skill_tt_per_hour else "",
            f"{skill_points_total:.4f}",
        )

    def clear_sessions(self):
        if not messagebox.askyesno("Clear sessions", "Delete all saved session history?"):
            return
        self.sessions = []
        save_json(SESSIONS_FILE, self.sessions)
        self.refresh_sessions_table()
        self.show_session_details(None)
        self.refresh_visible_session_views()

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
        self.refresh_visible_session_views()

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
            **self.state,
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
            "ui_theme": self.ui_style_var.get(),
            "ui_style": self.ui_style_var.get(),
            "ui_color_scheme": self.ui_color_scheme_var.get(),
            "sync_start_mode": self.sync_start_mode_var.get(),
            "projection_profession": self.session_projection_profession_var.get(),
            "projection_ped_cycle": self.session_projection_ped_var.get(),
            "analysis_efficiency": self.analysis_efficiency_var.get(),
            "analysis_animal_looter": self.analysis_looter_vars["Animal"].get(),
            "analysis_robot_looter": self.analysis_looter_vars["Robot"].get(),
            "analysis_mutant_looter": self.analysis_looter_vars["Mutant"].get(),
        }
        save_json(TRACKER_STATE_FILE, self.state)

    def on_close(self):
        self.cancel_profession_cell_edit()
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
