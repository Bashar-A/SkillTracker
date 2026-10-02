"""Correctness and work-count regressions for large histories (no timing gates)."""
import copy
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import entropia_tracker_ui as tracker
import test_loot_tracker as loot_fixture


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.app = tracker.EntropiaTrackerApp.__new__(tracker.EntropiaTrackerApp)
        self.source = loot_fixture.saved_session([
            loot_fixture.loot_event("2026-10-01 19:45:00", "A", 10),
            loot_fixture.loot_event("2026-10-01 19:46:00", "B", 20),
        ])
        self.source["loot_event_grouping_version"] = tracker.LOOT_EVENT_GROUPING_VERSION

    def test_normalization_is_reused_and_does_not_mutate_original(self):
        before = copy.deepcopy(self.source)
        with patch.object(self.app, "sanitize_loot_events", wraps=self.app.sanitize_loot_events) as normalize:
            summary = self.app.session_loot_summary(self.source)
            self.app.calculate_session_combat_metrics(self.source)
            self.app.loot_events_for_session(self.source)
            self.assertIs(self.app.session_loot_summary(self.source), summary)
            self.assertEqual(normalize.call_count, 1)
        self.assertEqual(self.source, before)

    def test_replaced_and_appended_lists_invalidate_cached_totals(self):
        self.assertEqual(self.app.session_loot_summary(self.source)["loot_total"], 30)
        self.source["loot_events"] = self.source["loot_events"][:1]
        self.assertEqual(self.app.session_loot_summary(self.source)["loot_total"], 10)
        self.source["loot_events"].append(loot_fixture.loot_event("", "C", 5))
        self.assertEqual(self.app.session_loot_summary(self.source)["loot_total"], 15)

    def test_cache_eviction_keeps_compact_summaries_and_pruning_releases_sources(self):
        self.app.session_cache = tracker.SessionDerivedCache(event_limit=2)
        sources = [copy.deepcopy(self.source) for _ in range(5)]
        for source in sources:
            self.app.session_loot_summary(source)
        self.assertEqual(len(self.app.session_cache.events), 2)
        with patch.object(self.app, "sanitize_loot_events", side_effect=AssertionError("history reread")):
            self.assertEqual(self.app.session_loot_summary(sources[0])["loot_total"], 30)
        self.app.session_cache.prune([sources[0]])
        self.assertEqual(len(self.app.session_cache.entries), 1)
        self.assertEqual(len(self.app.session_cache.events), 0)

    def test_exact_shot_counts_do_not_scan_or_reconstruct_raw_history(self):
        class UnreadableHistory(list):
            def __iter__(self):
                raise AssertionError("raw event history was scanned")
        weapon = next(name for name in tracker.WEAPONS if tracker.hunting_setup_cost_per_shot_ped(name) > 0)
        self.source.update(weapon=weapon, attacks_total=4, count_hunting=True, events=UnreadableHistory([{}]))
        for row in self.source["loot_events"]:
            row["shots"] = 2
        with patch.object(self.app, "reconstruct_loot_events_from_events", side_effect=AssertionError("regrouped")):
            result = self.app.recalculate_saved_session_turnover(self.source, self.source)
        self.assertAlmostEqual(result["ped_cycled"], 4 * tracker.hunting_setup_cost_per_shot_ped(weapon))


class HistoryPerformanceTkTests(unittest.TestCase):
    setUp = loot_fixture.LootTrackerTkTests.setUp
    destroy_root = loot_fixture.LootTrackerTkTests.destroy_root

    def large_session(self, count=450):
        source = self.app.sessions[0]
        origin = datetime(2026, 10, 1, 19, 45)
        rows, raw = [], []
        for i in range(count):
            timestamp = (origin + timedelta(seconds=i * 15)).strftime("%Y-%m-%d %H:%M:%S")
            row = loot_fixture.loot_event(timestamp, "Item A", i + 1, quantity=2, cost=10)
            row.update(index=i + 1, shots=2)
            rows.append(row)
            raw.append({"type": "loot", "timestamp": timestamp, "value_ped": i + 1,
                        "message": f"receipt-{i}"})
        weapon = next(name for name in tracker.WEAPONS if tracker.hunting_setup_cost_per_shot_ped(name) > 0)
        source.update(loot_events=rows, events=raw, loot_event_grouping_version=tracker.LOOT_EVENT_GROUPING_VERSION,
                      loot_ped_total=sum(row["value_ped"] for row in rows), loot_event_count=count,
                      count_hunting=True, attacks_total=count * 2, weapon=weapon)
        self.app.derived_cache().invalidate(source)
        self.app.notebook.select(self.app.sessions_tab)
        self.app.refresh_sessions_table()
        self.app.sessions_tree.selection_set("session_0")
        self.root.update()
        return source

    def test_hidden_views_wait_for_activation_and_log_pages_reach_every_event(self):
        source = self.large_session()
        with patch.object(self.app, "show_session_details", wraps=self.app.show_session_details) as details, \
             patch.object(self.app, "refresh_loot_tab", wraps=self.app.refresh_loot_tab) as loot:
            self.app.on_session_selected()
            self.assertEqual(details.call_count, 0)
            self.assertEqual(loot.call_count, 0)
            self.app.notebook.select(self.app.session_details_tab)
            self.root.update()
            self.assertEqual(details.call_count, 1)
            self.assertEqual(loot.call_count, 0)
        self.assertEqual(self.app.detail_events_pager.total, 450)
        text = self.app.session_detail_events_text.get("1.0", "end-1c")
        self.assertIn("receipt-199", text)
        self.assertNotIn("receipt-200", text)
        self.app.detail_events_pager.go(2)
        self.assertIn("receipt-449", self.app.session_detail_events_text.get("1.0", "end-1c"))
        self.assertEqual(len(source["events"]), 450)

    def test_loot_paging_sort_and_exclusion_use_original_event_identity(self):
        source = self.large_session()
        self.app.analysis_sessions = [copy.deepcopy(source)]
        self.app.notebook.select(self.app.loot_tab)
        self.root.update()
        self.assertEqual(len(self.app.loot_events_tree.get_children()), 200)
        self.assertEqual(self.app.loot_pager.controls.total, 450)
        self.app.loot_pager.controls.number.set("3")
        self.app.loot_pager.controls.jump()
        self.assertEqual(len(self.app.loot_events_tree.get_children()), 50)
        self.assertEqual(self.app.loot_events_tree.set("loot_event_449", "loot"), "450.0000")
        # Sort all 450 records, rather than only the current page.
        self.app.toggle_tree_sort(self.app.loot_events_tree, "loot")
        self.app.toggle_tree_sort(self.app.loot_events_tree, "loot")
        self.assertEqual(self.app.loot_events_tree.get_children()[0], "loot_event_449")
        self.app.loot_events_tree.selection_set("loot_event_449")
        self.app.exclude_selected_loot_events()
        self.assertTrue(source["loot_events"][449]["excluded_from_loot"])
        self.assertEqual(self.app.session_loot_summary(source)["kills"], 449)
        result = self.app.build_mob_analysis_results(50, {"Animal": 0})[0]
        self.assertEqual(result["tt_loot"], sum(range(1, 450)))
        self.assertEqual(result["loot_events"], 449)
        self.assertEqual(result["ped_cycled"], source["ped_cycled"])

    def test_refresh_normalizes_once_and_reopening_or_markup_never_reparses(self):
        source = self.large_session()
        with patch.object(self.app, "sanitize_loot_events", wraps=self.app.sanitize_loot_events) as normalize:
            self.app.refresh_loot_tab()
            self.assertEqual(normalize.call_count, 1)
            self.app.notebook.select(self.app.loot_tab)
            self.root.update()
            self.app.refresh_loot_tab(force=False)
            self.app.loot_markups["Item A"] = 150
            self.app.refresh_loot_tab(force=False)
            self.assertEqual(normalize.call_count, 1)
        self.assertIn("150.00%", self.app.loot_summary_var.get())
        self.assertEqual(self.app.session_loot_summary(source)["loot_total"], sum(range(1, 451)))

    def test_analysis_efficiency_looter_and_fixed_mu_use_cached_quantities(self):
        source = self.large_session()
        self.app.analysis_sessions = [copy.deepcopy(source)]
        self.app.build_mob_analysis_results(50, {"Animal": 20})
        with patch.object(self.app, "sanitize_loot_events", side_effect=AssertionError("reparsed")), \
             patch.object(self.app, "loot_item_value_totals", side_effect=AssertionError("recounted")):
            self.app.loot_markups = {}
            self.app.market_weekly_markups = {"Item A": {"type": "fixed", "value": 5}}
            result = self.app.build_mob_analysis_results(80, {"Animal": 30})[0]
        self.assertAlmostEqual(result["after_mu"], sum(range(1, 451)) + 450 * 2 * 5)
        self.assertAlmostEqual(result["expected_tt"], 86 + 7 * .8 + 7 * .3)

    def test_equipment_repricing_reuses_item_totals_in_history_and_analysis(self):
        source = self.large_session()
        self.app.analysis_sessions = [copy.deepcopy(source)]
        original = self.app.session_loot_summary(source)
        self.app.build_mob_analysis_results(50, {"Animal": 20})
        self.app.hunting_setups = {"Saved": {"weapon": source["weapon"]}}
        with patch.object(self.app, "loot_item_value_totals", side_effect=AssertionError("recounted")), \
             patch.object(self.app, "reconstruct_loot_events_from_events", side_effect=AssertionError("regrouped")):
            self.assertTrue(self.app.assign_saved_session_choice([source], "setup", "Saved"))
            self.app.build_mob_analysis_results(60, {"Animal": 40})
        self.assertIs(self.app.session_loot_summary(source), original)
        self.assertEqual(self.app.analysis_sessions[0]["ped_cycled"], source["ped_cycled"])
        self.assertEqual(self.app.analysis_sessions[0]["loot_events"][449]["cost_ped"], source["loot_events"][449]["cost_ped"])

    def test_mob_change_preserves_manual_turnover_and_updates_archived_metrics(self):
        source = self.large_session()
        self.app.analysis_sessions = [copy.deepcopy(source)]
        self.app.favorite_mobs = [{"mob": "Carabok", "maturity": "Puny"}]
        original = copy.deepcopy(source)
        with patch.object(self.app, "recalculate_saved_session_turnover", side_effect=AssertionError("repriced")):
            self.assertTrue(self.app.assign_saved_session_choice([source], "mob", ("Carabok", "Puny")))
        self.assertEqual(source["ped_cycled"], original["ped_cycled"])
        self.assertEqual(source["loot_events"], original["loot_events"])
        self.assertEqual(self.app.calculate_session_combat_metrics(source),
                         self.app.calculate_session_combat_metrics(self.app.analysis_sessions[0]))

    def test_archive_sync_copies_only_changed_sessions(self):
        source = self.large_session(2)
        second = copy.deepcopy(source)
        second["id"] = "second"
        self.app.sessions.append(second)
        self.app.analysis_sessions = copy.deepcopy(self.app.sessions)
        unchanged = self.app.analysis_sessions[1]
        staged = [{**source, "notes": "edited"}, second]
        with patch.object(self.app, "analysis_session_copy", wraps=self.app.analysis_session_copy) as copied:
            self.assertTrue(self.app.save_session_updates(staged, loot_changed=False))
            self.assertEqual(copied.call_count, 1)
        self.assertIs(self.app.analysis_sessions[1], unchanged)
        self.assertEqual(self.app.analysis_sessions[0]["notes"], "edited")

    def test_synchronized_archive_needs_no_startup_copy_but_stale_fields_are_repaired(self):
        source = self.large_session(2)
        self.app.analysis_sessions = [copy.deepcopy(source)]
        archived = self.app.analysis_sessions[0]
        archived["analysis_extension"] = "keep"
        archived["loot_events"][0]["analysis_event_extension"] = "keep too"
        with patch.object(self.app, "analysis_session_copy", wraps=self.app.analysis_session_copy) as copied:
            self.assertIs(self.app.synchronized_analysis_sessions(self.app.sessions)[0], archived)
            self.assertEqual(copied.call_count, 0)
            archived["loot_events"][0]["excluded_from_loot"] = True
            repaired = self.app.synchronized_analysis_sessions(self.app.sessions)[0]
            self.assertEqual(copied.call_count, 1)
        self.assertNotIn("excluded_from_loot", repaired["loot_events"][0])
        self.assertEqual(repaired["analysis_extension"], "keep")
        self.assertEqual(repaired["loot_events"][0]["analysis_event_extension"], "keep too")

    def test_metadata_edit_cannot_inherit_stale_archive_loot_totals(self):
        source = self.large_session(2)
        self.app.analysis_sessions = [copy.deepcopy(source)]
        self.app.analysis_sessions[0]["loot_events"][0]["value_ped"] = 999
        self.app.build_mob_analysis_results(50, {"Animal": 0})
        staged = [{**source, "notes": "edited"}]
        self.assertTrue(self.app.save_session_updates(staged, loot_changed=False))
        source.update(staged[0])
        result = self.app.build_mob_analysis_results(50, {"Animal": 0})[0]
        self.assertEqual(result["tt_loot"], 3)

    def test_refresh_cannot_redirect_selection_after_session_indices_shift(self):
        template = self.large_session(2)
        second = copy.deepcopy(template)
        second["id"] = "second"
        self.app.sessions.append(second)
        self.app.refresh_sessions_table()
        self.app.sessions_tree.selection_set("session_0")
        del self.app.sessions[0]
        self.app.refresh_sessions_table()
        self.assertEqual(self.app.sessions_tree.selection(), ())

    def test_latest_loot_can_be_excluded_when_previous_sessions_shows_an_older_page(self):
        template = copy.deepcopy(self.large_session(2))
        self.app.sessions = [copy.deepcopy({**template, "id": f"session-{i}"}) for i in range(250)]
        self.app.refresh_sessions_table()
        self.app.sessions_pager.controls.go(1)
        self.assertFalse(self.app.sessions_tree.exists("session_249"))
        self.assertEqual(self.app.sessions_tree.selection(), ())
        self.app.notebook.select(self.app.loot_tab)
        self.root.update()
        latest = self.app.sessions[-1]
        self.assertIs(self.app.loot_source_session(), latest)
        self.assertTrue(self.app.change_loot_exclusions(latest, [2]))
        self.assertEqual(latest["loot_ped_total"], 1)
        self.assertEqual(self.app.loot_pager.controls.total, 1)

    def test_item_drop_dialog_pages_and_releases_widgets_when_closed(self):
        self.large_session()
        self.app.notebook.select(self.app.loot_tab)
        self.root.update()
        iid = next(iid for iid, name in self.app.loot_item_summary_iid_to_name.items() if name == "Item A")
        self.app.loot_item_summary_tree.selection_set(iid)
        self.app.open_loot_item_details()
        window = next(child for child in self.root.children.values() if isinstance(child, tk.Toplevel))
        pager = next(pager for tree, pager in self.app.paged_trees.items() if tree.winfo_toplevel() is window)
        self.assertEqual(len(pager.tree.get_children()), 200)
        pager.controls.go(2)
        self.assertEqual(pager.tree.set("drop_449", "event"), "450")
        tree = pager.tree
        window.destroy()
        self.assertNotIn(tree, self.app.paged_trees)
        self.assertNotIn(tree, self.app.tree_heading_titles)

    def test_paged_loot_rows_and_controls_remain_visible_in_all_appearances(self):
        self.large_session()
        self.app.notebook.select(self.app.loot_tab)
        for style in tracker.UI_STYLES:
            for scheme in tracker.UI_COLOR_SCHEMES:
                self.app.ui_style_var.set(style)
                self.app.ui_color_scheme_var.set(scheme)
                self.app.apply_ui_theme()
                self.root.geometry("1000x760")
                self.root.update()
                bbox = self.app.loot_events_tree.bbox("loot_event_0")
                self.assertTrue(bbox, (style, scheme))
                self.assertLessEqual(bbox[1] + bbox[3], self.app.loot_events_tree.winfo_height(), (style, scheme))
                checkbox = self.app.loot_items_scrollable.winfo_children()[0]
                self.assertGreaterEqual(self.app.loot_items_canvas.winfo_height(), checkbox.winfo_height(), (style, scheme))
                controls = self.app.loot_pager.controls
                self.assertGreaterEqual(controls.winfo_width(), controls.winfo_reqwidth())

    def test_old_sessions_beyond_200_are_accessible_and_viewing_writes_no_files(self):
        template = copy.deepcopy(self.app.sessions[0])
        self.app.sessions = [{**template, "id": f"history-{i}", "notes": f"note-{i}"} for i in range(450)]
        self.app.notebook.select(self.app.sessions_tab)
        self.app.refresh_sessions_table()
        self.root.update()
        self.assertEqual(len(self.app.sessions_tree.get_children()), 200)
        self.app.sessions_pager.controls.go(2)
        self.assertEqual(len(self.app.sessions_tree.get_children()), 50)
        self.app.sessions_tree.selection_set("session_0")
        self.app.notebook.select(self.app.session_details_tab)
        self.root.update()
        self.assertIs(self.app.selected_session_from_table(), self.app.sessions[0])
        self.assertIn("note-0", self.app.session_detail_summary_var.get())
        self.assertEqual(tracker.SESSIONS_FILE.read_bytes(), self.before)

    def test_bulk_sort_keeps_numeric_order_empty_last_and_selection(self):
        tree = tk.ttk.Treeview(self.root, columns=("value",), show="headings")
        self.app.make_tree_sortable(tree, {"value": "Value"})
        for iid, value in (("a", "20 PED"), ("b", "3 PED"), ("c", "")):
            tree.insert("", "end", iid=iid, values=(value,))
        tree.selection_set("b")
        self.app.toggle_tree_sort(tree, "value")
        self.assertEqual(tree.get_children(), ("b", "a", "c"))
        self.assertEqual(tree.selection(), ("b",))
        self.app.toggle_tree_sort(tree, "value")
        self.assertEqual(tree.get_children(), ("a", "b", "c"))
        tree.destroy()


if __name__ == "__main__":
    unittest.main()

