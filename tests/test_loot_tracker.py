"""Regression checks for MU totals, local clock axes and legacy loot sessions."""
import copy
import json
import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entropia_tracker_ui as tracker


@contextmanager
def local_timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def loot_event(timestamp, item, value, quantity=1, cost=50):
    return {
        "started_at": timestamp, "ended_at": timestamp, "value_ped": value,
        "cost_ped": cost, "items": {item: quantity},
    }


def saved_session(events):
    # No message details or skill snapshots: older sessions remain readable.
    return {
        "id": "old-loot", "started_at": "2026-10-02T12:00:00",
        "ped_cycled": 100, "damage_total": 32000, "mob": "Carabok", "maturity": "Puny",
        "loot_events": events, "loot_ped_total": sum(row["value_ped"] for row in events),
        "skill_tt_curve_version": tracker.SKILL_TT_CURVE_VERSION,
        "notes": "Preserve my notes", "unknown_old_field": {"keep": [1, 2, 3]},
    }


@unittest.skipUnless(hasattr(time, "tzset"), "TZ-controlled tests require POSIX; GUI tests also run on Windows")
class LootClockTests(unittest.TestCase):
    def setUp(self):
        self.app = tracker.EntropiaTrackerApp.__new__(tracker.EntropiaTrackerApp)

    def test_utc_log_is_shown_as_local_clock(self):
        event = loot_event("2026-10-01 19:45:00", "Test item", 1)
        with local_timezone("Europe/Moscow"):
            origin = self.app.loot_event_chart_time(event)
            self.assertEqual(self.app._time_axis_label(0, time_origin=origin), "22:45")
            self.assertEqual(self.app._time_axis_label(15, time_origin=origin), "23:00")
            self.assertEqual(self.app.format_chart_time_label(origin, 1), "22:45:00")

    def test_midnight_labels_have_dates_and_duration_stays_elapsed(self):
        origin = datetime(2026, 10, 1, 20, 45, tzinfo=timezone.utc)
        with local_timezone("Europe/Moscow"):
            self.assertEqual(self.app._selection_time_text(0, 30, True, origin), "01.10 23:45 - 02.10 00:15 (30:00)")
            self.assertEqual(self.app._time_axis_label(1470, time_origin=origin, include_date=True), "03.10 00:15")

    def test_dst_changes_clock_without_changing_elapsed_spacing(self):
        origin = datetime(2026, 3, 29, 0, 45, tzinfo=timezone.utc)
        with local_timezone("Europe/Berlin"):
            self.assertEqual(self.app._time_axis_label(0, time_origin=origin), "01:45")
            self.assertEqual(self.app._time_axis_label(30, time_origin=origin), "03:15")

    def test_explicit_offsets_and_invalid_ended_at_are_supported(self):
        event = {"started_at": "2026-10-01T22:45:00+03:00", "ended_at": "invalid"}
        original = copy.deepcopy(event)
        self.assertEqual(self.app.loot_event_chart_time(event), datetime(2026, 10, 1, 19, 45, tzinfo=timezone.utc))
        self.assertEqual(event, original)
        self.assertIsNone(self.app.loot_event_chart_time({}))

    def test_historical_import_origin_is_loot_time(self):
        events = [loot_event("2026-10-01 19:50:00", "A", 1), loot_event("2026-10-01 19:45:00", "B", 1)]
        origin = self.app.first_loot_event_time(events)
        self.assertEqual(origin, datetime(2026, 10, 1, 19, 45, tzinfo=timezone.utc))
        self.assertEqual(self.app.elapsed_minutes(origin, self.app.loot_event_chart_time(events[0]), 1), 5)


class LootTrackerTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        # Tk's timer queue can outlive a destroyed root across test cases.
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.addCleanup(self.destroy_root)
        original_cwd = os.getcwd()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(temp.name)
        self.events = [loot_event("2026-10-01 19:45:00", "Item A", 70), loot_event("2026-10-01 20:15:00", "Item B", 30)]
        self.session = saved_session(self.events)
        tracker.SESSIONS_FILE.write_text(json.dumps([self.session]), encoding="utf-8")
        self.before = tracker.SESSIONS_FILE.read_bytes()
        self.app = tracker.EntropiaTrackerApp(self.root)
        self.app.loot_markups = {"Item B": 150}
        self.app.notebook.select(self.app.loot_tab)
        self.root.update()
        self.app.refresh_loot_tab()

    def destroy_root(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def canvas_texts(self, canvas):
        return [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]

    def test_weighted_mu_and_dpp_match_session_totals(self):
        summary = self.app.loot_summary_var.get()
        self.assertIn("Average overall MU (TT-weighted): 115.00%", summary)
        self.assertIn("Loot after MU: 115.0000", summary)
        self.assertIn("DPP: 3.200", summary)
        self.assertIn("Effective DPP: 0.002", summary)
        self.assertNotIn("Top items", summary)
        self.assertEqual(self.app.sessions[0], self.session)
        self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), self.before)

    def test_manual_mu_takes_priority_and_item_filter_does_not_change_total(self):
        self.app.market_weekly_markups = {"Item B": {"type": "percentage", "value": 300}}
        self.app.loot_item_vars["Item A"].set(True)
        self.app.refresh_loot_tab()
        self.assertIn("115.00%", self.app.loot_summary_var.get())
        self.app.loot_markups = {}
        self.app.refresh_loot_tab()
        self.assertIn("Average overall MU (TT-weighted): 160.00%", self.app.loot_summary_var.get())

    def test_fixed_markup_is_included_in_overall_mu(self):
        self.app.sessions[0]["loot_events"] = [loot_event("2026-10-01 19:45:00", "Item A", 10, quantity=2)]
        self.app.loot_markups = {}
        self.app.market_weekly_markups = {"Item A": {"type": "fixed", "value": 5}}
        self.app.refresh_loot_tab()
        self.assertIn("Average overall MU (TT-weighted): 200.00%", self.app.loot_summary_var.get())

    def test_empty_or_unattributed_loot_has_no_misleading_mu(self):
        for events in ([], [{"value_ped": 10, "started_at": "2026-10-01 19:45:00"}]):
            with self.subTest(events=events):
                self.app.sessions[0]["loot_events"] = events
                self.app.refresh_loot_tab()
                self.assertIn("Average overall MU (TT-weighted): —", self.app.loot_summary_var.get())

    def test_legacy_undated_sessions_use_event_numbers(self):
        for event in self.app.sessions[0]["loot_events"]:
            event.pop("started_at")
            event.pop("ended_at")
        original = copy.deepcopy(self.app.sessions[0])
        self.app.refresh_loot_tab()
        for canvas in (self.app.loot_value_time_canvas, self.app.loot_items_time_canvas):
            payload = self.app.loot_chart_payloads[canvas]
            self.assertFalse(payload["x_is_time"])
            self.assertEqual(payload["x_label"], "Loot event #")
        self.assertEqual(self.app.sessions[0], original)

    def test_partial_timestamps_do_not_invent_clock_times(self):
        self.app.sessions[0]["loot_events"].insert(0, loot_event("bad timestamp", "Item A", 5))
        self.app.refresh_loot_tab()
        points = self.app.loot_chart_payloads[self.app.loot_value_time_canvas]["points"]
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0]["y"], 75.0)
        self.assertEqual(points[0]["x"], 0)

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ-controlled rendering requires POSIX")
    def test_rendered_clock_ticks_zoom_and_resize(self):
        with local_timezone("Europe/Moscow"):
            self.app.refresh_loot_tab()
            canvas = self.app.loot_value_time_canvas
            self.assertIn("22:45", self.canvas_texts(canvas))
            self.assertIn("23:15", self.canvas_texts(canvas))
            self.assertNotIn("0:00", self.canvas_texts(canvas))
            self.app.loot_zoom_ranges[canvas] = (10, 30)
            self.app.redraw_chart_canvas(canvas)
            self.assertIn("22:55", self.canvas_texts(canvas))
            selection = self.app.describe_loot_chart_selection(canvas, 0, 30)
            self.assertIn("22:45 - 23:15 (30:00)", selection)
            self.root.geometry("1000x760")
            self.root.update()
            self.assertIn("22:55", self.canvas_texts(canvas))
            self.app.reset_loot_zoom()
            self.assertIn("22:45", self.canvas_texts(canvas))
            # The cost scatter plot keeps its numeric PED axis.
            self.assertFalse(self.app.loot_chart_payloads[self.app.loot_cost_canvas]["x_is_time"])
            self.app.loot_graphs_canvas.yview_moveto(1)
            self.root.update()
            self.assertGreater(self.app.loot_items_time_canvas.winfo_height(), 170)
            self.assertLess(self.app.loot_items_time_canvas.winfo_rooty(), 760)

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ-controlled rendering requires POSIX")
    def test_rendered_midnight_labels_and_item_bucket_totals(self):
        self.app.sessions[0]["loot_events"] = [
            loot_event("2026-10-01 20:45:00", "Item A", 50, quantity=2),
            loot_event("2026-10-01 21:15:00", "Item A", 50, quantity=3),
        ]
        with local_timezone("Europe/Moscow"):
            self.app.refresh_loot_tab()
            for canvas in (self.app.loot_value_time_canvas, self.app.loot_items_time_canvas):
                labels = self.canvas_texts(canvas)
                self.assertIn("01.10 23:45", labels)
                self.assertIn("02.10 00:15", labels)
            points = self.app.loot_chart_payloads[self.app.loot_items_time_canvas]["series"][0][1]
            self.assertEqual(sum(point["y"] for point in points), 5)
            self.assertEqual([point["x"] for point in points], [0, 30])


if __name__ == "__main__":
    unittest.main()

