"""Run with python -m unittest discover -s tests -v.

Tk integration tests run when a display is available; calculation tests are
headless. All file writes use a temporary directory, never real session data.
"""
import copy
import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entropia_tracker_ui as tracker


TEST_MOBS = {"Test Mob": {"maturities": {"Young": {"hp": 100}, "Mature": {"hp": 200}, "Unknown": {"hp": None}}}}


class LiveMonitorCalculations(unittest.TestCase):
    def setUp(self):
        self.app = tracker.EntropiaTrackerApp.__new__(tracker.EntropiaTrackerApp)
        self.app.current_skills = {"Rifle": 1000.0}
        self.tcl = tk.Tcl()
        self.app.session_projection_profession_var = tk.StringVar(self.tcl, value="Animal Looter")
        self.app.session_projection_ped_var = tk.StringVar(self.tcl, value="1000")
        self.session = tracker.MonitorSession(
            id="test", started_at="2026-10-01T10:00:00", mob="Test Mob", maturity="Young",
            ped_cycled=10.0, damage_total=3200.0,
            loot_events=[{"value_ped": 0.8} for _ in range(30)],
        )
        self.mob_patch = patch.object(tracker, "MOBS", TEST_MOBS)
        self.mob_patch.start()
        self.addCleanup(self.mob_patch.stop)

    def test_observed_damage_and_hp_dpp_use_pec(self):
        values = self.app.calculate_session_combat_metrics(self.session)
        self.assertAlmostEqual(values["dpp"], 3.2)
        self.assertAlmostEqual(values["effective_dpp"], 3.0)
        self.assertAlmostEqual(values["cost_per_kill"], 1 / 3)

    def test_effective_dpp_uses_session_maturity(self):
        self.app.mob_var = tk.StringVar(self.tcl, value="Different setup")
        self.app.maturity_var = tk.StringVar(self.tcl, value="Mature")
        self.assertAlmostEqual(self.app.calculate_session_combat_metrics(self.session)["effective_dpp"], 3.0)
        self.session.maturity = "Mature"
        self.assertAlmostEqual(self.app.calculate_session_combat_metrics(self.session)["effective_dpp"], 6.0)

    def test_no_ped_cycled_has_no_ratios(self):
        for ped in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(ped=ped):
                self.session.ped_cycled = ped
                values = self.app.calculate_session_combat_metrics(self.session)
                self.assertIsNone(values["dpp"])
                self.assertIsNone(values["effective_dpp"])
                self.assertIsNone(values["cost_per_kill"])

    def test_no_kills_keeps_observed_dpp(self):
        self.session.loot_events = []
        values = self.app.calculate_session_combat_metrics(self.session)
        self.assertAlmostEqual(values["dpp"], 3.2)
        self.assertIsNone(values["effective_dpp"])

    def test_unknown_mob_or_hp_does_not_invent_effective_dpp(self):
        for mob, maturity in (("Missing", "Young"), ("Test Mob", "Missing"), ("Test Mob", "Unknown")):
            with self.subTest(mob=mob, maturity=maturity):
                self.session.mob, self.session.maturity = mob, maturity
                self.assertIsNone(self.app.calculate_session_combat_metrics(self.session)["effective_dpp"])

    def test_zero_damage_is_a_valid_dpp(self):
        self.session.damage_total = 0
        self.assertEqual(self.app.calculate_session_combat_metrics(self.session)["dpp"], 0)

    def test_legacy_session_metrics_are_read_only(self):
        legacy = {
            "mob": "Test Mob", "maturity": "Young", "ped_cycled": 10, "damage_total": 3200,
            "loot_event_count": 30, "jammed_attacks": 2, "notes": "Keep this note",
            "custom_data": {"unknown_field": [1, 2]},
        }
        before = copy.deepcopy(legacy)
        self.assertAlmostEqual(self.app.calculate_session_combat_metrics(legacy)["effective_dpp"], 3.0)
        self.assertEqual(legacy, before)
        self.assertIsNone(self.app.calculate_session_combat_metrics({})["dpp"])

    def test_projection_rejects_invalid_and_nonfinite_amounts(self):
        for value in ("", "abc", "-1", "NaN", "inf", "1e999"):
            with self.subTest(value=value):
                self.app.session_projection_ped_var.set(value)
                self.assertIsNone(self.app.selected_projection_ped_cycle())
                self.assertIn("enter a finite", self.app.profession_projection_text(self.session))
        self.app.session_projection_ped_var.set("0")
        self.assertEqual(self.app.selected_projection_ped_cycle(), 0)

    def test_projection_changes_with_profession_and_amount_without_changing_data(self):
        self.session.skill_gains_tt = {"Rifle": 0.05}
        before = asdict(self.session)
        with patch.object(tracker, "PROFESSIONS", {"First": {"skills": {"Rifle": 100}}, "Second": {"skills": {"Rifle": 50}}}):
            self.app.session_projection_profession_var.set("First")
            self.app.session_projection_ped_var.set("10")
            first = self.app.calculate_profession_projection(self.session, "First", 10)
            second = self.app.calculate_profession_projection(self.session, "Second", 10)
            doubled = self.app.calculate_profession_projection(self.session, "First", 20)
            self.assertGreater(first, 0)
            self.assertAlmostEqual(first, second * 2)
            self.assertGreater(doubled, first)
            self.assertIn("First projected gain at 10 PED", self.app.profession_projection_text(self.session))
        self.assertEqual(asdict(self.session), before)


class LiveMonitorTkIntegration(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(self.destroy_root)
        self.original_cwd = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.temp.name)
        # Missing start snapshots make these pre-curve sessions ineligible for
        # the existing TT migration. Rendering/saving must retain every field.
        self.legacy = {
            "id": "legacy", "started_at": "2025-01-01T12:00:00", "jammed_attacks": 2,
            "skill_gains_points": {"Rifle": 1.0}, "skill_gains_tt": {"Rifle": 0.001},
            "ped_cycled": 10, "damage_total": 3000, "loot_ped_total": 9,
            "notes": "Original notes", "custom_data": {"keep": [1, 2]},
        }
        tracker.SESSIONS_FILE.write_text(json.dumps([self.legacy]), encoding="utf-8")
        tracker.ANALYSIS_SESSIONS_FILE.write_text(json.dumps([self.legacy]), encoding="utf-8")
        tracker.TRACKER_STATE_FILE.write_text(json.dumps({
            "last_log_read_at": "2025-01-01T12:00:00", "custom_setting": {"keep": True},
        }), encoding="utf-8")
        self.app = tracker.EntropiaTrackerApp(self.root)
        self.root.update()

    def destroy_root(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_legacy_files_unchanged_and_projection_preferences_survive_restart(self):
        sessions_before = tracker.SESSIONS_FILE.read_bytes()
        analysis_before = tracker.ANALYSIS_SESSIONS_FILE.read_bytes()
        self.app.sessions_tree.selection_set(self.app.sessions_tree.get_children()[0])
        self.app.refresh_selected_session_details()
        self.app.session_projection_profession_var.set("Robot Looter")
        self.app.session_projection_ped_var.set("2500.5")
        self.app.refresh_projection_views()
        self.app.save_state()
        state = tracker.load_json(tracker.TRACKER_STATE_FILE, {})
        self.assertEqual(state["projection_profession"], "Robot Looter")
        self.assertEqual(state["projection_ped_cycle"], "2500.5")
        self.assertEqual(state["last_log_read_at"], "2025-01-01T12:00:00")
        self.assertEqual(state["custom_setting"], {"keep": True})
        self.destroy_root()
        self.root = tk.Tk()
        self.app = tracker.EntropiaTrackerApp(self.root)
        self.assertEqual(self.app.session_projection_profession_var.get(), "Robot Looter")
        self.assertEqual(self.app.session_projection_ped_var.get(), "2500.5")
        self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), sessions_before)
        self.assertEqual(tracker.ANALYSIS_SESSIONS_FILE.read_bytes(), analysis_before)

    def test_live_event_pipeline_saves_legacy_history_and_resets_dashboard(self):
        self.app.current_session = tracker.MonitorSession(
            id="new", started_at="2026-10-01T12:00:00", count_hunting=True,
            mob="Test Mob", maturity="Young", notes="New notes",
        )
        self.app.monitoring = True
        timestamp = "2026-10-01 12:00:00"
        with patch.object(tracker, "MOBS", TEST_MOBS), patch.object(tracker, "hunting_setup_cost_per_shot_ped", return_value=0.05):
            for message in (
                "You inflicted 20 points of damage", "Critical hit - Additional damage! You inflicted 50 points of damage",
                "You missed", "You received Shrapnel Value: 0.1 PED",
                "You inflicted 130 points of damage", "You received Shrapnel Value: 0.2 PED",
            ):
                self.app.apply_event(tracker.ChatLogParser.parse_line(f"{timestamp} [System] [] {message}"))
            self.app.update_session_summary()
            self.assertEqual(self.app.monitor_metric_vars["dpp"].get(), "10.000")
            self.assertEqual(self.app.monitor_metric_vars["effective_dpp"].get(), "10.000")
            self.assertEqual(self.app.monitor_metric_vars["kills"].get(), "2")
            self.app.stop_sync()
        saved = tracker.load_json(tracker.SESSIONS_FILE, [])
        self.assertEqual(saved[0], self.legacy)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["notes"], "New notes")
        self.assertEqual(len(saved[1]["events"]), 6)
        self.assertAlmostEqual(saved[1]["ped_cycled"], 0.2)
        self.assertNotIn("effective_dpp", saved[1])
        self.assertEqual(self.app.monitor_metric_vars["dpp"].get(), "—")


if __name__ == "__main__":
    unittest.main()

