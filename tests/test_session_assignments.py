"""Editing historical equipment/targets must preserve recorded session data."""
import copy
import tkinter as tk
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import test_hunting_setup as hunting_fixture
import skill_tracker_ui as tracker


class SessionAssignmentTests(unittest.TestCase):
    destroy_root = hunting_fixture.HuntingSetupTests.destroy_root
    restart = hunting_fixture.HuntingSetupTests.restart

    def setUp(self):
        hunting_fixture.HuntingSetupTests.setUp(self)
        self.app.sessions[0].update(
            weapon=self.weapon_b, amplifier="", attachments=[], count_hunting=False,
            skill_gains_points={"Rifle": .1}, skill_gains_tt={"Rifle": .002},
            skill_gain_events_by_skill={"Rifle": 1}, skill_gain_tt_total=.002,
            damage_total=32000, attacks_total=90, loot_ped_total=80,
            loot_event_grouping_version=tracker.LOOT_EVENT_GROUPING_VERSION,
            loot_events=[{"started_at": "2026-10-01 12:00:00", "value_ped": 80,
                          "cost_ped": 100, "items": {"Wool": 20}, "extension": "keep"}],
            events=[{"type": "normal_hit", "damage": 32000}],
            current_skills_at_start={"Rifle": 1}, current_skills_at_end={"Rifle": 1.1},
        )
        second = copy.deepcopy(self.app.sessions[0])
        second.update(id="second", notes="Second session")
        self.app.sessions.append(second)
        self.app.analysis_sessions = copy.deepcopy(self.app.sessions)
        self.app.analysis_sessions[0]["analysis_extension"] = {"keep": True}
        tracker.save_json(tracker.SESSIONS_FILE, self.app.sessions)
        tracker.save_json(tracker.ANALYSIS_SESSIONS_FILE, self.app.analysis_sessions)
        self.app.notebook.select(self.app.sessions_tab)
        self.app.refresh_sessions_table()
        self.root.update()
        self.before = copy.deepcopy(self.app.sessions)
        self.analysis_before = copy.deepcopy(self.app.analysis_sessions)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.callback_errors, []))

    def descendants(self, parent):
        for child in list(parent.children.values()):
            yield child
            yield from self.descendants(child)

    def picker(self, kind):
        self.app.sessions_tree.selection_set("session_0")
        self.app.open_session_assignment(kind)
        self.root.update()
        window = next(w for w in self.root.children.values() if isinstance(w, tk.Toplevel))
        tree = next(w for w in self.descendants(window) if w.winfo_class() == "Treeview")
        save = next(w for w in self.descendants(window)
                    if w.winfo_class() == "TButton" and w.cget("text") == "Save to selected sessions")
        return window, tree, save

    def test_equipment_patch_keeps_history_target_extensions_and_live_setup(self):
        self.app.current_session = tracker.MonitorSession(id="live", started_at="2026-10-02T08:00:00",
                                                        ped_cycled=12, weapon=self.weapon_b)
        live = asdict(self.app.current_session)
        self.app.amplifier_var.set("Unfinished live setup")
        other_files = {p: p.read_bytes() for p in (tracker.HUNTING_SETUPS_FILE,
                       tracker.FAVORITE_MOBS_FILE, tracker.TRACKER_STATE_FILE)}
        self.app.toggle_tree_sort(self.app.sessions_tree, "notes")
        self.assertTrue(self.app.assign_saved_session_choice([self.app.sessions[0]], "setup", "Old equipment"))
        expected = {**self.before[0], "weapon": self.weapon_a, "amplifier": self.amplifier,
                    "attachments": [self.attachment]}
        self.assertEqual(self.app.sessions, [expected, self.before[1]])
        self.assertEqual(tracker.load_json(tracker.SESSIONS_FILE, []), [expected, self.before[1]])
        self.assertEqual(self.app.analysis_sessions[0], {**self.analysis_before[0],
                         "weapon": self.weapon_a, "amplifier": self.amplifier, "attachments": [self.attachment]})
        self.assertEqual(self.app.analysis_sessions[1], self.analysis_before[1])
        self.assertEqual(asdict(self.app.current_session), live)
        self.assertEqual(self.app.amplifier_var.get(), "Unfinished live setup")
        self.assertEqual(self.app.sessions_tree.selection(), ("session_0",))
        for path, content in other_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(tracker.load_json(tracker.SESSIONS_FILE.with_suffix(".json.bak"), []), self.before)

    def test_bulk_target_patch_keeps_equipment_and_survives_restart(self):
        self.assertTrue(self.app.assign_saved_session_choice(list(self.app.sessions), "mob", ("Other Mob", "Young")))
        expected = [{**s, "mob": "Other Mob", "maturity": "Young"} for s in self.before]
        self.assertEqual(self.app.sessions, expected)
        self.assertEqual(self.app.analysis_sessions, [{**s, "mob": "Other Mob", "maturity": "Young"}
                                                     for s in self.analysis_before])
        self.assertEqual(set(self.app.sessions_tree.selection()), {"session_0", "session_1"})
        self.restart()
        self.assertEqual(self.app.sessions, expected)
        self.assertEqual(self.app.analysis_sessions[0]["analysis_extension"], {"keep": True})

    def test_missing_choices_and_removed_session_do_not_save(self):
        saved = tracker.SESSIONS_FILE.read_bytes()
        with patch.object(tracker.messagebox, "showwarning") as warning:
            self.assertFalse(self.app.assign_saved_session_choice([self.app.sessions[0]], "setup", "Missing"))
            self.assertFalse(self.app.assign_saved_session_choice([self.app.sessions[0]], "mob", ("Other Mob", "Missing")))
            self.assertFalse(self.app.assign_saved_session_choice([copy.deepcopy(self.app.sessions[0])], "mob", ("Other Mob", "Young")))
            self.assertEqual(warning.call_count, 3)
        self.assertEqual(self.app.sessions, self.before)
        self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), saved)

    def test_primary_write_failure_leaves_files_and_memory_unchanged(self):
        saved = tracker.SESSIONS_FILE.read_bytes()
        with patch.object(tracker, "save_json", side_effect=OSError("disk full")), \
             patch.object(tracker.messagebox, "showerror") as error:
            self.assertFalse(self.app.assign_saved_session_choice([self.app.sessions[0]], "setup", "Old equipment"))
            error.assert_called_once()
        self.assertEqual(self.app.sessions, self.before)
        self.assertEqual(self.app.analysis_sessions, self.analysis_before)
        self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), saved)

    def test_analysis_write_failure_can_retry_without_losing_extensions(self):
        original_save = tracker.save_json
        def fail_analysis(path, content):
            if path == tracker.ANALYSIS_SESSIONS_FILE:
                raise OSError("disk full")
            original_save(path, content)
        with patch.object(tracker, "save_json", side_effect=fail_analysis), \
             patch.object(tracker.messagebox, "showerror"):
            self.assertFalse(self.app.assign_saved_session_choice([self.app.sessions[0]], "mob", ("Other Mob", "Young")))
        self.assertEqual(self.app.analysis_sessions, self.analysis_before)
        self.assertTrue(self.app.assign_saved_session_choice([self.app.sessions[0]], "mob", ("Other Mob", "Young")))
        self.assertEqual(tracker.load_json(tracker.ANALYSIS_SESSIONS_FILE, [])[0],
                         {**self.analysis_before[0], "mob": "Other Mob", "maturity": "Young"})

    def test_cancel_and_search_in_both_themes_never_change_session_files(self):
        for theme in ("Compact", "Command"):
            self.app.ui_theme_var.set(theme)
            self.app.apply_ui_theme()
            for kind in ("setup", "mob"):
                saved = tracker.SESSIONS_FILE.read_bytes()
                window, tree, save = self.picker(kind)
                self.assertTrue(save.instate(["disabled"]))
                search = next(w for w in self.descendants(window) if w.winfo_class() == "TEntry")
                search.insert(0, "Other" if kind == "setup" else "Young")
                self.root.update()
                self.assertEqual(len(tree.get_children()), 1)
                tree.selection_set(tree.get_children()[0])
                self.root.update()
                self.assertFalse(save.instate(["disabled"]))
                self.assertEqual(window.cget("background"), tracker.UI_THEMES[theme]["background"])
                window.destroy()
                self.assertEqual(self.app.sessions, self.before)
                self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), saved)

    def test_picker_save_applies_exact_favorite_maturity(self):
        window, tree, save = self.picker("mob")
        iid = next(i for i in tree.get_children() if tree.item(i, "values") == ("Other Mob", "Young"))
        tree.selection_set(iid)
        self.root.update()
        save.invoke()
        self.root.update()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.app.sessions[0], {**self.before[0], "mob": "Other Mob", "maturity": "Young"})

    def test_empty_selection_or_source_lists_show_guidance_without_dialog(self):
        with patch.object(tracker.messagebox, "showwarning") as warning:
            self.app.open_session_assignment("setup")
            self.app.sessions_tree.selection_set("session_0")
            self.app.hunting_setups = {}
            self.app.favorite_mobs = []
            self.app.open_session_assignment("setup")
            self.app.open_session_assignment("mob")
            self.assertEqual(warning.call_count, 3)
        self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.children.values()))

    def test_double_click_routes_clicked_session_after_sorting(self):
        tree = self.app.sessions_tree
        self.app.toggle_tree_sort(tree, "notes")
        tree.selection_set(("session_0", "session_1"))
        self.root.update()
        for column, kind in (("weapon", "setup"), ("mob", "mob")):
            x, y, width, height = tree.bbox("session_1", column)
            with patch.object(self.app, "open_session_assignment") as open_picker:
                self.app.on_sessions_tree_double_click(SimpleNamespace(x=x+width//2, y=y+height//2))
                open_picker.assert_called_once_with(kind)
            self.assertEqual(tree.selection(), ("session_1",))


if __name__ == "__main__":
    unittest.main()
