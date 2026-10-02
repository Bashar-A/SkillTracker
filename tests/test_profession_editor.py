"""Calculator previews recalculate bidirectionally and persist only on Save."""
import copy
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_hunting_setup as hunting_fixture
import skill_tracker_ui as tracker


class ProfessionCalculationTests(unittest.TestCase):
    def setUp(self):
        self.app = tracker.SkillTrackerApp.__new__(tracker.SkillTrackerApp)
        self.data = dict(current=1000., delta=0., weight=10.)

    def test_delta_new_skill_gain_and_profession_gain_resolve_same_row(self):
        delta = tracker.skill_tt_value(1200.5) - tracker.skill_tt_value(1000)
        for column, value in (('delta', delta), ('new', 1200.5), ('skill_gain', 200.5), ('profession_gain', 20.05)):
            row = self.app.calculate_profession_skill_row('Rifle', self.data, column, value)
            self.assertAlmostEqual(row['current'], 1000)
            self.assertAlmostEqual(row['new'], 1200.5)
            self.assertAlmostEqual(row['delta'], delta)
            self.assertAlmostEqual(row['skill_gain'], 200.5)
            self.assertAlmostEqual(row['profession_gain'], 20.05)
        self.assertEqual(self.data, dict(current=1000., delta=0., weight=10.))

    def test_current_edit_keeps_tt_delta_and_recalculates_other_values(self):
        self.data['delta'] = tracker.skill_tt_value(1200) - tracker.skill_tt_value(1000)
        row = self.app.calculate_profession_skill_row('Rifle', self.data, 'current', 1100.)
        self.assertAlmostEqual(tracker.skill_tt_value(row['new']) - tracker.skill_tt_value(1100), self.data['delta'])
        self.assertAlmostEqual(row['skill_gain'], row['new'] - 1100)
        self.assertAlmostEqual(row['profession_gain'], row['skill_gain'] / 10)

    def test_attribute_inverse_profession_gain_includes_twenty_times_weight(self):
        row = self.app.calculate_profession_skill_row('Agility', dict(current=100., delta=0., weight=5.), 'profession_gain', 2.)
        self.assertAlmostEqual(row['new'], 102)
        self.assertAlmostEqual(row['skill_gain'], 2)
        self.assertAlmostEqual(row['profession_gain'], 2)

    def test_negative_gain_supported_but_invalid_values_rejected(self):
        row = self.app.calculate_profession_skill_row('Rifle', self.data, 'new', 900.)
        self.assertLess(row['delta'], 0)
        self.assertEqual(row['skill_gain'], -100)
        for column, value in (('new', -1.), ('current', -1.), ('delta', -100000.), ('new', 20001.),
                              ('delta', float('inf')), ('new', float('nan'))):
            with self.assertRaises(ValueError):
                self.app.calculate_profession_skill_row('Rifle', self.data, column, value)

    def test_unchanged_legacy_values_above_curve_remain_readable(self):
        row = self.app.calculate_profession_skill_row('Rifle', dict(current=25000., delta=0., weight=10.))
        self.assertEqual(row['new'], 25000)
        self.assertEqual(row['profession_gain'], 0)

    def test_direct_point_value_on_flat_tt_segment_survives_recalculation(self):
        row = self.app.calculate_profession_skill_row('Rifle', self.data, 'new', 1200.)
        self.assertEqual(tracker.skill_tt_value(1199), tracker.skill_tt_value(1200))
        again = self.app.calculate_profession_skill_row('Rifle', row)
        self.assertEqual(again['new'], 1200)
        self.assertEqual(again['skill_gain'], 200)


class ProfessionEditorTests(unittest.TestCase):
    destroy_root = hunting_fixture.HuntingSetupTests.destroy_root

    def setUp(self):
        hunting_fixture.HuntingSetupTests.setUp(self)
        self.app.current_skills = {'Skinning': 1000., 'Butchering': 2000., 'Intelligence': 100., 'Rifle': 500., 'Unknown historic skill': 7.5}
        tracker.save_current_skills(self.app.current_skills)
        self.app.profession_var.set('Animal Looter')
        self.app.load_profession()
        self.app.notebook.select(self.app.profession_tab)
        self.root.update()
        self.saved = tracker.CURRENT_SKILLS_FILE.read_bytes()
        self.current = copy.deepcopy(self.app.current_skills)
        self.sessions = {path: path.read_bytes() for path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE)}
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))

    def descendants(self, parent):
        for child in list(parent.children.values()):
            yield child
            yield from self.descendants(child)

    def open_cell(self, column):
        self.app.skill_tree.xview_moveto(0)
        self.root.geometry('1440x820')
        self.root.update()
        x, y, width, height = self.app.skill_tree.bbox('Skinning', column)
        self.app.on_profession_skill_double_click(SimpleNamespace(x=x+width//2, y=y+height//2))
        self.assertEqual(self.app.profession_cell_editor_meta, ('Skinning', column))
        return self.app.profession_cell_editor

    def test_edits_and_recalculation_do_not_mutate_current_skills_or_files(self):
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        self.app.calculate_profession_gain()
        self.app.refresh_profession_current_values()
        self.assertEqual(self.app.current_skills, self.current)
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)
        self.assertAlmostEqual(self.app.entries['Skinning']['new'], 1200)
        self.assertEqual(self.app.total_gain_var.get(), 'Total profession gain: 70.0000')
        for path, content in self.sessions.items():
            self.assertEqual(path.read_bytes(), content)

    def test_explicit_save_persists_new_values_and_preserves_other_skills(self):
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        # A live update arriving since the last table refresh must be retained.
        self.app.current_skills['Butchering'] = 2001.
        self.app.current_skills['Rifle'] = 501.
        with patch.object(tracker.messagebox, 'showinfo'):
            self.app.save_current_skills_from_table()
        saved = tracker.load_current_skills()
        self.assertAlmostEqual(saved['Skinning'], 1200)
        self.assertEqual(saved['Butchering'], 2001)
        self.assertEqual(saved['Rifle'], 501)
        self.assertEqual(saved['Unknown historic skill'], 7.5)
        self.assertFalse(self.app.profession_skill_drafts)
        self.assertEqual(self.app.entries['Skinning']['delta'], 0)
        self.assertEqual(self.app.total_gain_var.get(), 'Total profession gain: 0.0000')
        for path, content in self.sessions.items():
            self.assertEqual(path.read_bytes(), content)

    def test_drafts_survive_profession_and_tab_changes_with_new_weights(self):
        self.app.edit_profession_skill_value('Intelligence', 'new', 101.)
        other = next(name for name, data in tracker.PROFESSIONS.items()
                     if name != 'Animal Looter' and 'Intelligence' in data['skills'])
        self.app.profession_var.set(other)
        self.app.load_profession()
        self.assertAlmostEqual(self.app.entries['Intelligence']['new'], 101)
        self.assertAlmostEqual(self.app.entries['Intelligence']['profession_gain'], tracker.PROFESSIONS[other]['skills']['Intelligence'] / 5)
        self.app.notebook.select(self.app.monitor_tab)
        self.root.update()
        self.app.notebook.select(self.app.profession_tab)
        self.root.update()
        self.assertAlmostEqual(self.app.entries['Intelligence']['new'], 101)
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)

    def test_live_updates_refresh_clean_rows_without_overwriting_drafts(self):
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        self.app.current_skills.update(Skinning=1001., Butchering=2001.)
        self.app.refresh_profession_current_values()
        self.assertEqual(self.app.entries['Skinning']['current'], 1000)
        self.assertAlmostEqual(self.app.entries['Skinning']['new'], 1200)
        self.assertEqual(self.app.entries['Butchering']['current'], 2001)
        self.assertEqual(self.app.entries['Butchering']['new'], 2001)
        self.assertEqual(self.app.total_gain_var.get(), 'Total profession gain: 70.0000')

    def test_inline_editor_commit_cancel_and_invalid_input(self):
        for column, value in (('current', '1000'), ('delta', '0'), ('new', '1200'), ('skill_gain', '200'), ('profession_gain', '70')):
            editor = self.open_cell(column)
            editor.delete(0, 'end')
            editor.insert(0, value)
            self.assertTrue(self.app.commit_profession_cell_edit())
        row = copy.deepcopy(self.app.entries['Skinning'])
        editor = self.open_cell('new')
        editor.delete(0, 'end')
        editor.insert(0, '1500')
        self.app.cancel_profession_cell_edit()
        self.assertEqual(self.app.entries['Skinning'], row)
        editor = self.open_cell('new')
        editor.delete(0, 'end')
        editor.insert(0, 'unfinished input')
        with patch.object(tracker.messagebox, 'showerror'), patch.object(tracker, 'save_current_skills') as save:
            self.assertFalse(self.app.commit_profession_cell_edit())
            self.app.save_current_skills_from_table()
            save.assert_not_called()
        self.assertEqual(self.app.entries['Skinning'], row)
        self.app.cancel_profession_cell_edit()
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)

    def test_save_failure_preserves_preview_and_current_skills(self):
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        with patch.object(tracker, 'save_current_skills', side_effect=OSError('disk full')), \
             patch.object(tracker.messagebox, 'showerror'):
            self.app.save_current_skills_from_table()
        self.assertEqual(self.app.current_skills, self.current)
        self.assertAlmostEqual(self.app.entries['Skinning']['new'], 1200)
        self.assertIn('Skinning', self.app.profession_skill_drafts)
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)

    def test_reload_discards_drafts_and_close_does_not_save_preview(self):
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        with patch.object(tracker.messagebox, 'showinfo'):
            self.app.reload_saved_skills()
        self.assertEqual(self.app.entries['Skinning']['new'], 1000)
        self.assertFalse(self.app.profession_skill_drafts)
        self.app.edit_profession_skill_value('Skinning', 'new', 1200.)
        editor = self.open_cell('new')
        editor.delete(0, 'end')
        editor.insert(0, 'unfinished closing edit')
        with patch.object(self.root, 'destroy'):
            self.app.on_close()
        self.assertEqual(tracker.load_current_skills(), self.current)

    def test_scrolling_finishes_valid_editor_without_saving(self):
        editor = self.open_cell('new')
        editor.delete(0, 'end')
        editor.insert(0, '1200')
        self.app.scroll_profession_table(self.app.skill_tree.xview, 'scroll', 1, 'units')
        self.assertIsNone(self.app.profession_cell_editor)
        self.assertAlmostEqual(self.app.entries['Skinning']['new'], 1200)
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)

    def test_ui_labels_controls_and_theme_switch_keep_editor(self):
        for theme in ('Compact', 'Command'):
            self.app.ui_theme_var.set(theme)
            self.app.apply_ui_theme()
            self.root.update()
            self.assertEqual(self.app.skill_tree.heading('current', 'text'), 'Current value')
            self.assertEqual(self.app.skill_tree.heading('new', 'text'), 'New value')
            widgets = list(self.descendants(self.app.profession_tab))
            self.assertFalse(any(w.winfo_class() == 'TCheckbutton' for w in widgets))
            labels = [w.cget('text') for w in widgets if w.winfo_class() in ('TButton', 'TLabel', 'TLabelframe')]
            self.assertIn('100 profession gain = 1 profession level.', labels)
            self.assertNotIn('Calculate', labels)
            self.assertNotIn('Edit selected skill', labels)
            editor = self.open_cell('new')
            editor.delete(0, 'end')
            editor.insert(0, 'unfinished edit')
            self.app.ui_theme_var.set('Command' if theme == 'Compact' else 'Compact')
            self.app.apply_ui_theme()
            self.root.update()
            self.assertIs(self.app.profession_cell_editor, editor)
            self.assertEqual(editor.get(), 'unfinished edit')
            self.app.cancel_profession_cell_edit()


if __name__ == '__main__':
    unittest.main()
