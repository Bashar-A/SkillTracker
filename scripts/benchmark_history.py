"""Synthetic Tk benchmark, isolated from user files. Run from any directory.

Windows: python scripts/benchmark_history.py
Linux: run under a Tk-capable display (for example Xvfb).
--module can point to an older entropia_tracker_ui.py for before/after comparison.
Timings are medians; cached operations are warmed and exclude startup/import.
"""
import argparse
import copy
import gc
import importlib.util
import inspect
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime, timedelta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, default=Path(__file__).resolve().parents[1] / "entropia_tracker_ui.py")
    parser.add_argument("--receipts", type=int, default=10000)
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--receipts-per-session", type=int, default=200)
    parser.add_argument("--sort-rows", type=int, default=30000)
    args = parser.parse_args()
    if min(args.receipts, args.sessions, args.receipts_per_session, args.sort_rows) < 1:
        parser.error("dataset sizes must be positive")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    spec = importlib.util.spec_from_file_location("benchmark_tracker", args.module.resolve())
    tracker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tracker
    spec.loader.exec_module(tracker)
    weapon = next(name for name in tracker.WEAPONS if tracker.hunting_setup_cost_per_shot_ped(name) > 0)
    shot_cost = tracker.hunting_setup_cost_per_shot_ped(weapon)

    def session(count, session_id):
        raw, loot = [], []
        origin = datetime(2026, 9, 1)
        messages = ["You received Shrapnel Value: 2.70 PED", "You received Wool x 5 Value: 0.30 PED"]
        for i in range(count):
            stamp = (origin + timedelta(seconds=i * 15)).strftime("%Y-%m-%d %H:%M:%S")
            raw.extend({"type": "normal_hit", "timestamp": stamp, "damage": 10., "message": "Synthetic hit"} for _ in range(5))
            for item, quantity, value, message in zip(("Shrapnel", "Wool"), (27000, 5), (2.7, .3), messages):
                raw.append({"type": "loot", "timestamp": stamp, "item": item, "quantity": quantity,
                            "value_ped": value, "message": message})
            loot.append({"index": i + 1, "started_at": stamp, "ended_at": stamp,
                         "items": {"Shrapnel": 27000, "Wool": 5}, "messages": list(messages),
                         "value_ped": 3., "cost_ped": shot_cost * 5, "shots": 5})
        return {"id": session_id, "started_at": origin.isoformat(), "weapon": weapon, "amplifier": "",
                "attachments": [], "mob": "Carabok", "maturity": "Puny", "count_hunting": True,
                "attacks_total": count * 5, "damage_total": count * 50., "ped_cycled": count * 5 * shot_cost,
                "loot_ped_total": count * 3., "loot_event_count": count,
                "loot_event_grouping_version": tracker.LOOT_EVENT_GROUPING_VERSION,
                "skill_tt_curve_version": tracker.SKILL_TT_CURVE_VERSION, "loot_events": loot, "events": raw}

    def measure(operation):
        samples = []
        for _ in range(3):
            gc.collect()
            started = time.perf_counter()
            operation()
            samples.append(time.perf_counter() - started)
        return round(statistics.median(samples), 6)

    previous_cwd = os.getcwd()
    root = None
    temp = tempfile.TemporaryDirectory(prefix="skilltracker-benchmark-")
    try:
        os.chdir(temp.name)
        tracker.save_json(tracker.TRACKER_STATE_FILE, {"hunting_targets_imported": True})
        root = tk.Tk()
        app = tracker.EntropiaTrackerApp(root)
        root.update()
        source = session(args.receipts, "large")
        app.sessions = [source]
        app.refresh_sessions_table()
        app.sessions_tree.selection_set("session_0")
        app.notebook.select(app.sessions_tab)
        root.update()
        result = {"python": sys.version.split()[0], "receipts": args.receipts,
                  "raw_events": len(source["events"]), "seconds": {}}
        timing = result["seconds"]
        timing["select_with_loot_hidden"] = measure(app.on_session_selected)
        timing["details"] = measure(lambda: app.show_session_details(source))
        app.notebook.select(app.loot_tab)
        root.update()
        timing["explicit_loot_refresh"] = measure(app.refresh_loot_tab)
        cached_refresh = (lambda: app.refresh_loot_tab(force=False)) if "force" in inspect.signature(app.refresh_loot_tab).parameters else app.refresh_loot_tab
        timing["unchanged_loot_refresh"] = measure(cached_refresh)
        timing["reprice_calculation"] = measure(lambda: app.recalculate_saved_session_turnover(source, source))
        tree = tk.ttk.Treeview(root, columns=("value",), show="headings")
        app.make_tree_sortable(tree, {"value": "Value"})
        for i in range(args.sort_rows):
            tree.insert("", "end", iid=str(i), values=(args.sort_rows - i,))
        app.tree_sort_state[tree] = {"column": "value", "descending": False}
        def sort():
            app.tree_sort_state[tree]["descending"] = not app.tree_sort_state[tree]["descending"]
            app.apply_tree_sort(tree)
        timing["sort"] = measure(sort)
        result["sort_rows"] = args.sort_rows
        tree.destroy()
        template = session(args.receipts_per_session, "template")
        app.sessions = [copy.deepcopy({**template, "id": f"history-{i}"}) for i in range(args.sessions)]
        app.analysis_sessions = copy.deepcopy(app.sessions)
        app.refresh_sessions_table()
        levels = {"Animal": 0., "Robot": 0., "Mutant": 0.}
        started = time.perf_counter()
        app.build_mob_analysis_results(50., levels)
        timing["analysis_first_calculation"] = round(time.perf_counter() - started, 6)
        timing["analysis_cached"] = measure(lambda: app.build_mob_analysis_results(60., levels))
        timing["sessions_table_cached"] = measure(app.refresh_sessions_table)
        result.update(history_sessions=args.sessions, receipts_per_history_session=args.receipts_per_session)
        print(json.dumps(result, indent=2))
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()
        root = None
    finally:
        if root is not None:
            root.destroy()
        os.chdir(previous_cwd)
        temp.cleanup()


if __name__ == "__main__":
    main()

