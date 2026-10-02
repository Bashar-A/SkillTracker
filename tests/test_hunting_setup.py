"""Equipment and target switching must preserve legacy profiles and sessions."""
import copy
import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entropia_tracker_ui as tracker

MOBS = {
    "Test Mob": {"maturities": {"Young": {"hp": 10}, "Mature": {"hp": 20}}},
    "Other Mob": {"maturities": {"Young": {"hp": 30}}},
}


class HuntingSetupTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(self.destroy_root)
        original_cwd = os.getcwd()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(temp.name)
        mobs_patch = patch.object(tracker, "MOBS", MOBS)
        mobs_patch.start()
        self.addCleanup(mobs_patch.stop)
        weapons = list(tracker.WEAPONS)
        self.weapon_a, self.weapon_b = weapons[:2]
        self.amplifier = next(iter(tracker.AMPLIFIERS))
        self.attachment = next(iter(tracker.ATTACHMENTS))
        self.setups = {
            "Old equipment": {"weapon": self.weapon_a, "amplifier": self.amplifier,
                "attachments": [self.attachment], "mob": "Test Mob", "maturity": "Mature",
                "count_hunting": True, "extension": {"keep": [1, 2, 3]}},
            "Other equipment": {"weapon": self.weapon_b, "attachments": [],
                "mob": "Other Mob", "maturity": "Young", "count_hunting": False},
        }
        self.session = {"id": "old", "started_at": "2026-10-01T12:00:00",
            "mob": "Test Mob", "maturity": "Mature", "ped_cycled": 100,
            "skill_tt_curve_version": tracker.SKILL_TT_CURVE_VERSION,
            "notes": "Keep", "unknown_old_field": {"keep": True}}
        tracker.HUNTING_SETUPS_FILE.write_text(json.dumps(self.setups))
        tracker.FAVORITE_MOBS_FILE.write_text(json.dumps([{"mob": "Other Mob", "maturity": "Young", "extension": "keep"}]))
        tracker.TRACKER_STATE_FILE.write_text(json.dumps({"mob": "Other Mob", "maturity": "Young", "custom_setting": "keep"}))
        for path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE):
            path.write_text(json.dumps([self.session]))
        self.original_files = {path: path.read_bytes() for path in (tracker.HUNTING_SETUPS_FILE, tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE)}
        self.app = tracker.EntropiaTrackerApp(self.root)
        self.app.notebook.select(self.app.hunting_tab)
        self.root.update()

    def destroy_root(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def restart(self):
        self.destroy_root()
        self.root = tk.Tk()
        self.app = tracker.EntropiaTrackerApp(self.root)
        self.app.notebook.select(self.app.hunting_tab)
        self.root.update()

    def click_row(self, tree, iid):
        tree.see(iid)
        self.root.update()
        x, y, width, height = tree.bbox(iid)
        tree.event_generate("<ButtonPress-1>", x=x+10, y=y+height//2)
        tree.event_generate("<ButtonRelease-1>", x=x+10, y=y+height//2)
        self.root.update()

    def test_legacy_targets_import_once_without_rewriting_old_files(self):
        self.assertEqual(len(self.app.favorite_mobs), 2)
        self.assertEqual(self.app.favorite_mobs[0]["extension"], "keep")
        for path, data in self.original_files.items():
            self.assertEqual(path.read_bytes(), data)
        self.restart()
        self.assertEqual(len(self.app.favorite_mobs), 2)
        self.assertEqual(tracker.load_json(tracker.TRACKER_STATE_FILE, {})["custom_setting"], "keep")

    def test_one_click_equipment_switch_keeps_current_target_and_saved_profiles(self):
        iid = next(iid for iid, name in self.app.hunting_setup_iid_to_name.items() if name == "Old equipment")
        self.click_row(self.app.hunting_setups_tree, iid)
        self.assertEqual(self.app.weapon_var.get(), self.weapon_a)
        self.assertEqual(self.app.amplifier_var.get(), self.amplifier)
        self.assertEqual(self.app.selected_attachments(), [self.attachment])
        self.assertTrue(self.app.count_hunting_var.get())
        self.assertEqual((self.app.mob_var.get(), self.app.maturity_var.get()), ("Other Mob", "Young"))
        self.assertEqual(tracker.HUNTING_SETUPS_FILE.read_bytes(), self.original_files[tracker.HUNTING_SETUPS_FILE])
        # Return also activates a keyboard-selected row.
        iid = next(iid for iid, name in self.app.hunting_setup_iid_to_name.items() if name == "Other equipment")
        self.app.hunting_setups_tree.selection_set(iid)
        self.app.hunting_setups_tree.focus_set()
        self.app.hunting_setups_tree.event_generate("<Return>")
        self.root.update()
        self.assertEqual(self.app.weapon_var.get(), self.weapon_b)

    def test_one_click_favorite_switch_keeps_equipment_and_persists_current_target(self):
        self.app.hunting_setup_name_var.set("Old equipment")
        self.app.load_named_hunting_setup()
        equipment = copy.deepcopy(self.app.current_hunting_setup_payload())
        iid = next(iid for iid, index in self.app.favorite_mob_iid_to_index.items() if self.app.favorite_mobs[index]["mob"] == "Test Mob")
        self.click_row(self.app.favorite_mobs_tree, iid)
        self.assertEqual((self.app.mob_var.get(), self.app.maturity_var.get()), ("Test Mob", "Mature"))
        self.assertEqual(self.app.current_hunting_setup_payload(), equipment)
        self.restart()
        self.assertEqual((self.app.mob_var.get(), self.app.maturity_var.get()), ("Test Mob", "Mature"))
        self.assertEqual(self.app.current_hunting_setup_payload(), equipment)
        self.assertEqual(self.app.sessions, [self.session])

    def test_new_and_updated_setups_exclude_targets_keep_extensions(self):
        self.app.hunting_setup_name_var.set("New equipment")
        self.app.weapon_var.set(self.weapon_b)
        self.app.save_named_hunting_setup()
        saved = tracker.load_json(tracker.HUNTING_SETUPS_FILE, {})
        self.assertNotIn("mob", saved["New equipment"])
        self.assertNotIn("maturity", saved["New equipment"])
        self.app.hunting_setup_name_var.set("old EQUIPMENT")
        with patch.object(tracker.messagebox, "askyesno", return_value=True):
            self.app.save_named_hunting_setup()
        saved = tracker.load_json(tracker.HUNTING_SETUPS_FILE, {})
        self.assertEqual(saved["Old equipment"]["extension"], {"keep": [1, 2, 3]})
        self.assertNotIn("mob", saved["Old equipment"])
        self.assertEqual(saved["Other equipment"], self.setups["Other equipment"])
        self.assertEqual(len(self.app.favorite_mobs), 2)
        for path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE):
            self.assertEqual(path.read_bytes(), self.original_files[path])

    def test_favorites_deduplicate_pairs_allow_other_maturity_and_keep_removal(self):
        self.app.mob_var.set("Test Mob")
        self.app.maturity_var.set("Mature")
        self.app.add_current_favorite_mob()
        self.assertEqual(len(self.app.favorite_mobs), 2)
        self.app.maturity_var.set("Young")
        self.app.add_current_favorite_mob()
        self.assertEqual(len(self.app.favorite_mobs), 3)
        iid = next(iid for iid, index in self.app.favorite_mob_iid_to_index.items() if self.app.favorite_mobs[index].get("maturity") == "Mature")
        self.app.favorite_mobs_tree.selection_set(iid)
        with patch.object(tracker.messagebox, "askyesno", return_value=True):
            self.app.remove_selected_favorite_mob()
        self.restart()
        self.assertEqual(len(self.app.favorite_mobs), 2)
        self.assertFalse(any(row.get("maturity") == "Mature" for row in self.app.favorite_mobs))
        self.assertEqual(tracker.HUNTING_SETUPS_FILE.read_bytes(), self.original_files[tracker.HUNTING_SETUPS_FILE])

    def test_unknown_legacy_mob_is_retained_and_not_applied(self):
        self.app.favorite_mobs.append({"mob": "Unavailable mob", "maturity": "Old maturity", "extension": "keep"})
        self.app.refresh_favorite_mobs()
        self.app.favorite_mobs_tree.selection_set("favorite_2")
        with patch.object(tracker.messagebox, "showwarning") as warning:
            self.app.use_selected_favorite_mob()
        warning.assert_called_once()
        self.assertEqual(self.app.mob_var.get(), "Other Mob")
        self.assertEqual(self.app.favorite_mobs[-1]["extension"], "keep")

    def test_switches_do_not_rewrite_running_or_historical_sessions(self):
        self.app.current_session = tracker.MonitorSession(id="running", started_at="2026-10-01T15:00:00",
            weapon=self.weapon_b, mob="Other Mob", maturity="Young", ped_cycled=15, notes="Keep this session")
        original = copy.deepcopy(self.app.current_session)
        self.app.hunting_setup_name_var.set("Old equipment")
        self.app.load_named_hunting_setup()
        self.app.favorite_mobs_tree.selection_set("favorite_1")
        self.app.use_selected_favorite_mob()
        self.assertEqual(self.app.current_session, original)
        for path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE):
            self.assertEqual(path.read_bytes(), self.original_files[path])

    def test_delete_uses_selected_row_and_preserves_its_imported_target(self):
        iid = next(iid for iid, name in self.app.hunting_setup_iid_to_name.items() if name == "Old equipment")
        self.app.hunting_setups_tree.selection_set(iid)
        self.app.hunting_setup_name_var.set("Other equipment")
        with patch.object(tracker.messagebox, "askyesno", return_value=True):
            self.app.delete_named_hunting_setup()
        self.assertEqual(list(self.app.hunting_setups), ["Other equipment"])
        self.assertTrue(any(row["mob"] == "Test Mob" for row in self.app.favorite_mobs))

    def test_manual_target_edits_clear_unrelated_favorite_selection(self):
        self.app.mob_var.set("Test Mob")
        self.app.maturity_var.set("Young")
        self.app.on_hunting_changed()
        self.assertEqual(self.app.favorite_mobs_tree.selection(), ())
        self.app.maturity_var.set("Mature")
        self.app.on_hunting_changed()
        selected = self.app.favorite_mobs_tree.selection()[0]
        self.assertEqual(self.app.favorite_mobs[self.app.favorite_mob_iid_to_index[selected]]["maturity"], "Mature")


if __name__ == "__main__":
    unittest.main()

