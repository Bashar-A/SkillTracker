"""Theme switching must preserve widgets, edits, zoom and session files."""
import copy
import tkinter as tk
import unittest
from dataclasses import asdict

import entropia_tracker_ui as tracker
import test_hunting_setup as hunting_fixture

APPEARANCES = tuple((style, scheme) for style in tracker.UI_STYLES for scheme in tracker.UI_COLOR_SCHEMES)


class UIThemeTests(unittest.TestCase):
    # Reuse the temporary legacy-data fixture without rerunning its tests.
    setUp = hunting_fixture.HuntingSetupTests.setUp
    destroy_root = hunting_fixture.HuntingSetupTests.destroy_root
    restart = hunting_fixture.HuntingSetupTests.restart

    def switch(self, name, scheme=None):
        self.app.ui_style_var.set(name)
        if scheme is not None:
            self.app.ui_color_scheme_var.set(scheme)
        self.app.apply_ui_theme()
        self.root.update()

    def test_switch_preserves_unsaved_edits_selection_and_widget_identity(self):
        self.app.sessions_tree.selection_set("session_0")
        self.app.notebook.select(self.app.session_details_tab)
        self.root.update()
        widgets = (self.app.sessions_tree, self.app.session_projection_skill_tree,
                   self.app.weapon_combo, self.app.favorite_mobs_tree)
        self.app.amplifier_var.set("unfinished amplifier name")
        self.app.session_projection_ped_var.set("unfinished amount")
        self.app.hunting_setup_name_var.set("Unsaved preset")
        self.app.mob_filter_var.set("Unfinished search")
        for name, scheme in APPEARANCES:
            self.switch(name, scheme)
            self.assertEqual(self.app.notebook.select(), str(self.app.session_details_tab))
            self.assertEqual(self.app.sessions_tree.selection(), ("session_0",))
            self.assertEqual(self.app.amplifier_var.get(), "unfinished amplifier name")
            self.assertEqual(self.app.session_projection_ped_var.get(), "unfinished amount")
            self.assertEqual(self.app.hunting_setup_name_var.get(), "Unsaved preset")
            self.assertEqual(self.app.mob_filter_var.get(), "Unfinished search")
            self.assertEqual(widgets, (self.app.sessions_tree, self.app.session_projection_skill_tree,
                                      self.app.weapon_combo, self.app.favorite_mobs_tree))

    def test_navigation_styles_and_buttons_keep_same_tabs(self):
        tabs = self.app.notebook.tabs()
        self.switch("Command")
        self.assertEqual(self.app.sidebar.winfo_manager(), "grid")
        self.assertEqual(self.app.notebook.cget("style"), "Tracker.Sidebar.TNotebook")
        for tab in tabs:
            self.app.navigation_buttons[tab].invoke()
            self.root.update()
            self.assertEqual(self.app.notebook.select(), tab)
            self.assertEqual(self.app.navigation_buttons[tab].cget("style"), "Tracker.Selected.Nav.TButton")
        self.switch("Compact")
        self.assertEqual(self.app.sidebar.winfo_manager(), "")
        self.assertEqual(self.app.notebook.cget("style"), "TNotebook")
        self.assertEqual(self.app.notebook.tabs(), tabs)
        self.assertEqual(self.app.notebook.select(), tabs[-1])

    def test_theme_persists_and_only_preference_changes_in_saved_state(self):
        before = copy.deepcopy(self.app.state)
        self.switch("Command", "Light")
        saved = tracker.load_json(tracker.TRACKER_STATE_FILE, {})
        self.assertEqual(saved, {**before, "ui_theme": "Command", "ui_style": "Command", "ui_color_scheme": "Light"})
        self.restart()
        self.assertEqual(self.app.ui_theme_var.get(), "Command")
        self.assertEqual(self.app.ui_color_scheme_var.get(), "Light")
        self.assertEqual(self.app.notebook.cget("style"), "Tracker.Sidebar.TNotebook")
        self.assertEqual(self.root.cget("background"), tracker.UI_COLOR_SCHEMES["Light"]["background"])

    def test_legacy_default_and_invalid_preference_fall_back_to_compact(self):
        self.assertEqual(self.app.ui_theme_var.get(), "Compact")
        for invalid in ("future-unknown-theme", None, ["Compact"], {"name": "Command"}):
            self.app.state["ui_theme"] = invalid
            tracker.save_json(tracker.TRACKER_STATE_FILE, self.app.state)
            self.restart()
            self.assertEqual(self.app.ui_theme_var.get(), "Compact")
            self.assertEqual(self.app.sidebar.winfo_manager(), "")

    def test_style_and_color_controls_are_independent_and_all_four_choices_persist(self):
        self.assertEqual(self.app.style_combo.cget("values"), tracker.UI_STYLES)
        self.assertEqual(self.app.theme_combo.cget("values"), tuple(tracker.UI_COLOR_SCHEMES))
        for style, scheme in APPEARANCES:
            self.switch(style, scheme)
            self.assertEqual(self.root.cget("background"), tracker.UI_COLOR_SCHEMES[scheme]["background"])
            self.assertEqual(self.app.sidebar.winfo_manager(), "grid" if style == "Command" else "")
            self.restart()
            self.assertEqual(self.app.ui_style_var.get(), style)
            self.assertEqual(self.app.ui_color_scheme_var.get(), scheme)
            next_scheme = "Dark" if scheme == "Light" else "Light"
            self.app.ui_color_scheme_var.set(next_scheme)
            self.app.theme_combo.event_generate("<<ComboboxSelected>>")
            self.root.update()
            self.assertEqual(self.app.ui_style_var.get(), style)
            self.assertEqual(self.app.ui_color_scheme_var.get(), next_scheme)
            next_style = "Command" if style == "Compact" else "Compact"
            self.app.ui_style_var.set(next_style)
            self.app.style_combo.event_generate("<<ComboboxSelected>>")
            self.root.update()
            self.assertEqual(self.app.ui_color_scheme_var.get(), next_scheme)
            self.assertEqual(self.app.ui_style_var.get(), next_style)

    def test_legacy_command_stays_dark_on_upgrade_and_general_save_keeps_new_preferences(self):
        self.app.state.update(ui_theme="Command")
        tracker.save_json(tracker.TRACKER_STATE_FILE, self.app.state)
        self.restart()
        self.assertEqual(self.app.ui_style_var.get(), "Command")
        self.assertEqual(self.app.ui_color_scheme_var.get(), "Dark")
        self.switch("Compact", "Dark")
        self.app.save_state()
        saved = tracker.load_json(tracker.TRACKER_STATE_FILE, {})
        self.assertEqual(saved["ui_style"], "Compact")
        self.assertEqual(saved["ui_color_scheme"], "Dark")
        self.assertEqual(saved["ui_theme"], "Compact")
        self.restart()
        self.assertEqual(self.app.ui_color_scheme_var.get(), "Dark")
        self.assertEqual(self.app.sidebar.winfo_manager(), "")

    def test_running_session_legacy_files_and_favorites_are_unchanged(self):
        self.app.current_session = tracker.MonitorSession(id="live", started_at="2026-10-02T07:00:00",
            weapon=self.weapon_a, mob="Test Mob", maturity="Mature", ped_cycled=100,
            damage_total=32000, notes="Do not lose this")
        session = copy.deepcopy(asdict(self.app.current_session))
        paths = [*self.original_files, tracker.FAVORITE_MOBS_FILE]
        before = {p: p.read_bytes() for p in paths}
        for name, scheme in APPEARANCES:
            self.switch(name, scheme)
            self.assertEqual(asdict(self.app.current_session), session)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_open_dialog_text_and_native_combobox_popdown_recolor(self):
        self.app.sessions_tree.selection_set("session_0")
        self.app.notebook.select(self.app.session_details_tab)
        self.app.show_session_details(self.app.sessions[0])
        # Exercise a Tcl-owned popdown created before the theme change.
        combos = (self.app.style_combo, self.app.theme_combo, self.app.weapon_combo)
        for combo in combos:
            self.root.tk.call("ttk::combobox::PopdownWindow", str(combo))
        window = tk.Toplevel(self.root)
        text = tk.Text(window)
        text.insert("1.0", "Unsaved dialog text")
        text.pack()
        for name, scheme in APPEARANCES:
            self.switch(name, scheme)
            colors = tracker.UI_COLOR_SCHEMES[scheme]
            self.assertEqual(text.get("1.0", "end-1c"), "Unsaved dialog text")
            self.assertEqual(text.cget("background"), colors["surface"])
            self.assertEqual(text.cget("foreground"), colors["ink"])
            self.assertEqual(text.cget("insertbackground"), colors["ink"])
            for combo in combos:
                self.assertEqual(self.root.tk.call(f"{combo}.popdown.f.l", "cget", "-background"), colors["field"])
        window.destroy()

    def test_chart_zoom_payload_and_filter_survive_theme_change(self):
        self.app.current_session = tracker.MonitorSession(id="chart", started_at="2026-10-02T07:00:00",
            mob="Test Mob", maturity="Mature", ped_cycled=10, loot_ped_total=8,
            loot_events=[{"started_at": "2026-10-02 07:00:00", "value_ped": 3, "cost_ped": 5, "items": {"Wool": 10}},
                         {"started_at": "2026-10-02 07:10:00", "value_ped": 5, "cost_ped": 5, "items": {"Wool": 20}}])
        self.app.notebook.select(self.app.loot_tab)
        self.root.update()
        self.app.refresh_loot_tab()
        canvas = self.app.loot_value_time_canvas
        self.app.loot_zoom_ranges[canvas] = (0., 8.)
        payload = copy.deepcopy(self.app.loot_chart_payloads[canvas])
        self.app.loot_item_filter_var.set("Wool")
        for name, scheme in APPEARANCES:
            self.switch(name, scheme)
            self.assertEqual(self.app.loot_zoom_ranges[canvas], (0., 8.))
            self.assertEqual(self.app.loot_chart_payloads[canvas], payload)
            self.assertEqual(self.app.loot_item_filter_var.get(), "Wool")
            self.assertEqual(canvas.cget("background"), tracker.UI_COLOR_SCHEMES[scheme]["surface"])
            rectangle = next(i for i in canvas.find_all() if canvas.type(i) == "rectangle")
            self.assertEqual(canvas.itemcget(rectangle, "fill"), tracker.UI_COLOR_SCHEMES[scheme]["surface"])
            self.assertEqual(self.app.loot_cost_canvas.cget("background"), tracker.UI_COLOR_SCHEMES[scheme]["surface"])

    def test_repeated_switch_does_not_inflate_table_minimum_column_widths(self):
        tree = self.app.sessions_tree
        original = {column: tree.column(column, "minwidth") for column in tree["columns"]}
        self.root.geometry("1400x850")
        self.root.update()
        for name, scheme in APPEARANCES:
            self.switch(name, scheme)
        self.assertEqual({column: tree.column(column, "minwidth") for column in tree["columns"]}, original)


class SavedAppearanceTests(unittest.TestCase):
    def test_legacy_defaults_and_explicit_new_choices(self):
        for state, expected in (({}, ("Compact", "Light")),
                                ({"ui_theme": "Compact"}, ("Compact", "Light")),
                                ({"ui_theme": "Command"}, ("Command", "Dark")),
                                ({"ui_theme": "Command", "ui_style": "Compact", "ui_color_scheme": "Dark"}, ("Compact", "Dark")),
                                ({"ui_theme": "Command", "ui_style": "Command", "ui_color_scheme": "Light"}, ("Command", "Light"))):
            before = copy.deepcopy(state)
            self.assertEqual(tracker.saved_ui_appearance(state), expected)
            self.assertEqual(state, before)

    def test_invalid_preferences_are_validated_independently(self):
        for invalid in (None, "unknown", [], {}, 1):
            self.assertEqual(tracker.saved_ui_appearance({"ui_style": invalid, "ui_color_scheme": "Dark"}), ("Compact", "Dark"))
            self.assertEqual(tracker.saved_ui_appearance({"ui_style": "Command", "ui_color_scheme": invalid}), ("Command", "Light"))
            self.assertEqual(tracker.saved_ui_appearance({"ui_theme": invalid}), ("Compact", "Light"))


if __name__ == "__main__":
    unittest.main()

