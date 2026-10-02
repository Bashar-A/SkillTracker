"""Unwanted receipts must stay excluded across edits, restarts and analysis."""
import copy
import tkinter as tk
import unittest
from dataclasses import asdict
from unittest.mock import patch

import test_loot_tracker as loot_fixture
import test_session_assignments as assignment_fixture
import skill_tracker_ui as tracker


class LootExclusionTests(unittest.TestCase):
    destroy_root = loot_fixture.LootTrackerTkTests.destroy_root

    def setUp(self):
        loot_fixture.LootTrackerTkTests.setUp(self)
        self.source = self.app.sessions[0]
        self.app.sessions_tree.selection_set("session_0")
        self.source["loot_events"][0]["custom_event_data"] = {"keep": True}
        self.source["events"] = [{"type": "loot", "item": "Item A", "quantity": 1,
                                  "value_ped": 70, "timestamp": "2026-10-01 19:45:00"}]
        self.app.analysis_sessions = [copy.deepcopy(self.source)]
        self.app.analysis_sessions[0]["analysis_extension"] = {"keep": [1, 2]}
        self.app.analysis_sessions[0]["loot_events"][0]["analysis_event_extension"] = "keep"
        tracker.save_json(tracker.SESSIONS_FILE, self.app.sessions)
        tracker.save_json(tracker.ANALYSIS_SESSIONS_FILE, self.app.analysis_sessions)
        self.original = copy.deepcopy(self.source)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))

    def descendants(self, parent):
        for child in list(parent.children.values()):
            yield child
            yield from self.descendants(child)

    def assert_synced(self):
        archive = self.app.analysis_sessions[0]
        def values(session):
            return [(row.get("index"), row.get("items"), row.get("value_ped"), row.get("cost_ped"), row.get("messages"))
                    for row in self.app.loot_events_for_session(session)]
        self.assertEqual(values(archive), values(self.source))
        self.assertEqual(archive["loot_ped_total"], self.source["loot_ped_total"])
        self.assertEqual(archive["ped_cycled"], self.source["ped_cycled"])
        self.assertEqual(archive["analysis_extension"], {"keep": [1, 2]})
        self.assertEqual(archive["loot_events"][0]["analysis_event_extension"], "keep")
        self.assertEqual(tracker.load_json(tracker.SESSIONS_FILE, [])[0], self.source)
        self.assertEqual(tracker.load_json(tracker.ANALYSIS_SESSIONS_FILE, [])[0], archive)

    def test_exclude_event_recalculates_tables_graphs_mu_and_analysis(self):
        self.assertTrue(self.app.change_loot_exclusions(self.source, [2]))
        self.assertEqual(self.source["loot_ped_total"], 70)
        self.assertEqual(self.source["loot_event_count"], 1)
        self.assertEqual(self.source["ped_cycled"], 100)
        self.assertEqual(self.source["events"], self.original["events"])
        self.assertEqual(self.source["loot_events_before_exclusion"], self.original["loot_events"])
        included = self.app.loot_events_for_session(self.source)
        self.assertEqual(len(included), 1)
        self.assertEqual(included[0]["cost_ped"], 100)  # Removed loot does not erase shots.
        self.assertEqual(self.source["loot_events"][1]["value_ped"], 30)
        self.assertEqual(self.app.calculate_session_combat_metrics(self.source),
                         {"kills": 1, "cost_per_kill": 100, "dpp": 3.2, "effective_dpp": .001})
        self.assertIn("Average overall MU (TT-weighted): 100.00%", self.app.loot_summary_var.get())
        self.assertEqual(self.app.loot_events_tree.set("loot_event_0", "cost"), "100.0000")
        self.assertEqual(self.app.sessions_tree.set("session_0", "loot"), "70.0000")
        result = self.app.build_mob_analysis_results(50, {"Animal": 0, "Robot": 0, "Mutant": 0})[0]
        self.assertEqual(result["tt_loot"], 70)
        self.assertEqual(result["loot_events"], 1)
        self.assert_synced()

    def test_all_excluded_does_not_reappear_on_refresh_reload_or_restore(self):
        self.app.change_loot_exclusions(self.source, [1, 2])
        for _ in range(2):
            self.app.refresh_loot_tab()
            self.assertEqual(self.app.loot_events_for_session(self.source), [])
            self.assertEqual(self.app.calculate_session_combat_metrics(self.source)["kills"], 0)
        self.assertEqual(self.app.loot_events_tree.get_children(), ())
        loaded = tracker.load_json(tracker.SESSIONS_FILE, [])[0]
        self.assertEqual(self.app.loot_events_for_session(loaded), [])
        self.assertTrue(self.app.change_loot_exclusions(self.source, restore=True))
        self.assertEqual(self.source["loot_ped_total"], 100)
        self.assertEqual(self.source["loot_event_count"], 2)
        self.assertNotIn("excluded_from_loot", self.app.analysis_sessions[0]["loot_events"][0])
        self.assertTrue(self.app.restore_loot_button.instate(["disabled"]))
        self.assert_synced()

    def mixed_event(self):
        self.source["loot_events"][0].update(items={"Item A": 1, "Item B": 2}, value_ped=70,
            messages=["You received Item A Value: 10 PED", "You received Item B x 2 Value: 60 PED"])

    def test_specific_item_keeps_other_items_and_other_drops_of_same_name(self):
        self.mixed_event()
        self.app.analysis_sessions[0]["loot_events"][0].update(self.source["loot_events"][0])
        self.app.analysis_sessions[0] = self.app.analysis_session_copy(self.source, self.app.analysis_sessions[0])
        self.assertTrue(self.app.change_loot_exclusions(self.source, [1], "Item B"))
        included = self.app.loot_events_for_session(self.source)
        self.assertEqual(included[0]["items"], {"Item A": 1})
        self.assertEqual(included[0]["value_ped"], 10)
        self.assertEqual(included[0]["messages"], ["You received Item A Value: 10 PED"])
        self.assertEqual(included[1]["items"], {"Item B": 1})
        self.assertEqual(self.source["loot_ped_total"], 40)
        self.assertEqual(self.source["loot_event_count"], 2)
        self.assertEqual(self.app.loot_item_value_totals(included)["Item B"]["value_ped"], 30)
        self.assert_synced()

    def test_legacy_regrouping_keeps_original_extensions_and_same_second_kills(self):
        timestamp = "2026-10-01 19:45:00"
        events = []
        for name, value in (("Item A", 70), ("Item B", 30)):
            events.extend([{"type": "normal_hit", "timestamp": timestamp, "damage": 10},
                           {"type": "loot", "timestamp": timestamp, "item": name, "quantity": 1,
                            "value_ped": value, "message": f"You received {name} Value: {value} PED"}])
        self.source.update(events=events, loot_event_grouping_version=0,
                           loot_events=[{"started_at": timestamp, "value_ped": 100, "cost_ped": 100,
                                         "items": {"Item A": 1, "Item B": 1}, "legacy_extension": "keep"}])
        self.app.analysis_sessions = [copy.deepcopy(self.source)]
        self.app.analysis_sessions[0]["loot_events"][0]["analysis_legacy_extension"] = "keep too"
        self.assertEqual(len(self.app.loot_events_for_session(self.source)), 2)
        self.assertTrue(self.app.change_loot_exclusions(self.source, [2]))
        self.assertEqual(self.source["loot_ped_total"], 70)
        self.assertEqual(self.source["loot_events_before_exclusion"][0]["legacy_extension"], "keep")
        self.assertEqual(self.app.analysis_sessions[0]["loot_events_before_exclusion"][0]["analysis_legacy_extension"], "keep too")
        self.assertEqual(len(self.app.loot_events_for_session(self.source)), 1)
        self.assertEqual(self.source["events"], events)
        self.assertTrue(self.app.change_loot_exclusions(self.source, restore=True))
        self.assertEqual(len(self.app.loot_events_for_session(self.source)), 2)

    def test_legacy_mixed_event_without_messages_requires_whole_event(self):
        self.source["loot_events"][0]["items"]["Item B"] = 2
        before = copy.deepcopy(self.source)
        with patch.object(tracker.messagebox, "showwarning") as warning:
            self.assertFalse(self.app.change_loot_exclusions(self.source, [1], "Item B"))
            warning.assert_called_once()
        self.assertEqual(self.source, before)
        self.assertTrue(self.app.change_loot_exclusions(self.source, [1]))
        self.assertEqual(self.source["loot_ped_total"], 30)

    def test_primary_and_analysis_save_failure_leave_edit_unchanged(self):
        save = tracker.save_json
        before = tracker.SESSIONS_FILE.read_bytes()
        archive_before = tracker.ANALYSIS_SESSIONS_FILE.read_bytes()
        for failed_path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE):
            def fail(path, content):
                if path == failed_path:
                    raise OSError("disk full")
                save(path, content)
            with patch.object(tracker, "save_json", side_effect=fail), patch.object(tracker.messagebox, "showerror"):
                self.assertFalse(self.app.change_loot_exclusions(self.source, [1]))
            self.assertEqual(self.source, self.original)
            self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), before)
            self.assertEqual(tracker.ANALYSIS_SESSIONS_FILE.read_bytes(), archive_before)
        self.assertTrue(self.app.change_loot_exclusions(self.source, [1]))
        self.assert_synced()

    def test_archive_only_messages_cannot_override_recalculated_source_values(self):
        self.app.analysis_sessions[0]["loot_events"][0]["messages"] = ["You received Item A Value: 999 PED"]
        self.assertTrue(self.app.change_loot_exclusions(self.source, [2]))
        self.assertEqual(self.app.loot_events_for_session(self.app.analysis_sessions[0])[0]["value_ped"], 70)
        self.assertEqual(self.app.analysis_sessions[0]["loot_events_before_exclusion"][0]["messages"],
                         ["You received Item A Value: 999 PED"])
        self.assert_synced()

    def test_live_exclusion_later_same_second_loot_and_saved_snapshot(self):
        live = tracker.MonitorSession(id="live", started_at="2026-10-02T12:00:00", ped_cycled=100,
                                     damage_total=32000, mob="Carabok", maturity="Puny")
        self.app.current_session = live
        event = {"type": "loot", "timestamp": "2026-10-01 19:45:00", "item": "Item A",
                 "quantity": 1, "value_ped": 70, "message": "You received Item A Value: 70 PED"}
        self.app.apply_event(event)
        self.assertTrue(self.app.change_loot_exclusions(vars(live), [1]))
        self.assertEqual(live.loot_ped_total, 0)
        self.assertEqual(self.app.monitor_metric_vars["kills"].get(), "0")
        self.app.apply_event({**event, "item": "Item B", "value_ped": 30,
                             "message": "You received Item B Value: 30 PED"})
        self.app.refresh_live_ui(force=True)
        self.assertEqual(live.loot_ped_total, 30)
        self.assertEqual(live.loot_event_count, 1)
        self.assertEqual(len(live.loot_events), 2)
        snapshot = asdict(live)
        self.assertEqual([row["value_ped"] for row in self.app.loot_events_for_session(snapshot)], [30])
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertEqual(self.app.calculate_session_combat_metrics(live)["kills"], 1)
        self.assertTrue(self.app.change_loot_exclusions(vars(live), restore=True))
        self.assertEqual(live.loot_ped_total, 100)
        self.assertEqual(self.source, self.original)  # Active source takes precedence.
        self.app.change_loot_exclusions(vars(live), [1])
        self.app.monitoring = True
        self.app.stop_sync()
        saved = tracker.load_json(tracker.SESSIONS_FILE, [])[-1]
        self.assertEqual(saved["loot_ped_total"], 30)
        self.assertEqual([row["value_ped"] for row in self.app.loot_events_for_session(saved)], [30])

    def test_manual_ped_and_notes_use_same_archive_save_path(self):
        self.app.change_loot_exclusions(self.source, [2])
        for column, text in (("ped", "0"), ("ped", "200"), ("notes", "Corrected session")):
            entry = tk.ttk.Entry(self.root)
            entry.insert(0, text)
            self.app.session_cell_editor = entry
            self.app.session_cell_editor_meta = (0, column, "session_0")
            self.app.commit_session_cell_edit()
        self.assertEqual(self.source["ped_cycled"], 200)
        self.assertEqual(self.source["notes"], "Corrected session")
        self.assertEqual(self.app.loot_events_for_session(self.source)[0]["cost_ped"], 200)
        self.assertEqual(self.app.calculate_session_combat_metrics(self.source)["dpp"], 1.6)
        self.assert_synced()

    def test_unique_legacy_session_without_id_syncs_and_ambiguous_records_stay_untouched(self):
        self.source.pop("id")
        self.app.analysis_sessions[0].pop("id")
        self.assertTrue(self.app.change_loot_exclusions(self.source, [2]))
        self.assertEqual(self.app.analysis_sessions[0]["loot_ped_total"], 70)
        self.assertTrue(self.app.change_loot_exclusions(self.source, restore=True))
        self.app.sessions.append(copy.deepcopy(self.source))
        before = copy.deepcopy(self.app.analysis_sessions)
        self.app.change_loot_exclusions(self.source, [2])
        self.assertEqual(self.app.analysis_sessions, before)  # No guessed linkage.

    def test_startup_repairs_stale_analysis_without_touching_unrelated_archive(self):
        self.app.change_loot_exclusions(self.source, [2])
        unrelated = {**copy.deepcopy(self.original), "id": "archive-only", "notes": "keep"}
        tracker.save_json(tracker.ANALYSIS_SESSIONS_FILE, [self.original, unrelated])
        self.destroy_root()
        self.root = tk.Tk()
        self.app = tracker.SkillTrackerApp(self.root)
        self.source = self.app.sessions[0]
        self.assertEqual(self.app.analysis_sessions[0]["loot_ped_total"], 70)
        self.assertEqual(self.app.loot_events_for_session(self.app.analysis_sessions[0])[0]["value_ped"], 70)
        self.assertEqual(self.app.analysis_sessions[1], unrelated)
        self.assertEqual(tracker.load_json(tracker.ANALYSIS_SESSIONS_FILE, [])[0]["loot_ped_total"], 70)

    def test_event_and_item_history_buttons_work_in_both_themes(self):
        for theme in ("Compact", "Command"):
            self.app.ui_theme_var.set(theme)
            self.app.apply_ui_theme()
            self.app.refresh_loot_tab()
            self.app.loot_events_tree.selection_set("loot_event_0")
            self.app.open_loot_event_details()
            window = next(w for w in self.root.children.values() if isinstance(w, tk.Toplevel))
            tree = next(w for w in self.descendants(window) if w.winfo_class() == "Treeview")
            tree.selection_set(tree.get_children()[0])
            button = next(w for w in self.descendants(window) if w.winfo_class() == "TButton"
                          and w.cget("text") == "Exclude selected item from this event")
            button.invoke()
            self.assertFalse(window.winfo_exists())
            self.assertEqual(self.source["loot_ped_total"], 30)
            self.app.restore_loot_button.invoke()
            iid = next(iid for iid, name in self.app.loot_item_summary_iid_to_name.items() if name == "Item B")
            self.app.loot_item_summary_tree.selection_set(iid)
            self.app.open_loot_item_details()
            window = next(w for w in self.root.children.values() if isinstance(w, tk.Toplevel))
            tree = next(w for w in self.descendants(window) if w.winfo_class() == "Treeview")
            tree.selection_set(tree.get_children()[0])
            button = next(w for w in self.descendants(window) if w.winfo_class() == "TButton"
                          and w.cget("text") == "Exclude selected drops of this item")
            button.invoke()
            self.assertFalse(window.winfo_exists())
            self.assertEqual(self.source["loot_ped_total"], 70)
            self.app.restore_loot_button.invoke()

    def test_deleted_source_and_empty_selection_do_not_change_files(self):
        with patch.object(tracker.messagebox, "showwarning") as warning:
            self.app.exclude_selected_loot_events()
            self.assertFalse(self.app.change_loot_exclusions(copy.deepcopy(self.source), [1]))
            self.assertEqual(warning.call_count, 2)
        self.assertEqual(self.source, self.original)


class RepricedExcludedSessionTests(unittest.TestCase):
    setUp = assignment_fixture.SessionAssignmentTests.setUp
    destroy_root = assignment_fixture.SessionAssignmentTests.destroy_root
    restart = assignment_fixture.SessionAssignmentTests.restart

    def test_exclusion_survives_setup_mob_changes_and_restart(self):
        session = self.app.sessions[0]
        self.app.sessions_tree.selection_set("session_0")
        self.assertTrue(self.app.change_loot_exclusions(session, [1]))
        for kind, choice in (("setup", "Old equipment"), ("mob", ("Other Mob", "Young"))):
            self.assertTrue(self.app.assign_saved_session_choice([session], kind, choice))
            self.assertEqual(self.app.loot_events_for_session(session), [])
            self.assertEqual(session["loot_ped_total"], 0)
            self.assertEqual(self.app.calculate_session_combat_metrics(session)["kills"], 0)
            self.assertEqual(self.app.analysis_sessions[0]["ped_cycled"], session["ped_cycled"])
        self.restart()
        session = self.app.sessions[0]
        self.assertEqual(self.app.loot_events_for_session(session), [])
        self.assertEqual(self.app.analysis_sessions[0]["analysis_extension"], {"keep": True})
        self.app.change_loot_exclusions(session, restore=True)
        self.assertEqual(session["loot_ped_total"], 80)
        self.assertEqual(self.app.analysis_sessions[0]["loot_ped_total"], 80)
        self.assertEqual(self.app.loot_events_for_session(session)[0]["cost_ped"], session["ped_cycled"])


if __name__ == "__main__":
    unittest.main()
