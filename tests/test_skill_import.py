import copy
import json
import tempfile
import unittest
from tkinter import ttk
from pathlib import Path
from unittest.mock import patch
import entropia_tracker_ui as tracker
import test_profession_editor as fixture
from skill_import import parse_skill_import, read_skill_import

class SkillImportTests(unittest.TestCase):
    def test_flat_and_wrapped_skill_snapshots(self):
        raw = {'Skinning': 1234.5, 'Unknown skill': 0}
        self.assertEqual(parse_skill_import(raw), raw)
        self.assertEqual(parse_skill_import({'skills': raw}), raw)
    def test_bad_snapshot_never_silently_drops_a_value(self):
        for raw in ([], {}, {'skills': []}, {'Skinning': True}, {'Skinning': None}, {'Skinning': '123'}, {'Skinning': -1}, {'Skinning': float('nan')}, {'Skinning': float('inf')}, {'Skinning': 100001}, {'': 1}, {'Skinning': 1, ' skinning ': 2}):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_skill_import(raw)
    def test_utf8_bom_duplicates_and_size_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'skills.json'
            path.write_text('{"Skinning": 1234.5}', encoding='utf-8-sig')
            self.assertEqual(read_skill_import(path), {'Skinning': 1234.5})
            path.write_text('{"Skinning":1,"Skinning":2}', encoding='utf-8')
            with self.assertRaises(ValueError): read_skill_import(path)
            with patch('skill_import.MAX_SKILLS_BYTES', 5), self.assertRaises(ValueError): read_skill_import(path)
    def test_legacy_launcher_import_is_the_same_application(self):
        import skill_tracker_ui as legacy
        self.assertIs(legacy, tracker); self.assertIs(legacy.SkillTrackerApp, tracker.EntropiaTrackerApp)

class SkillImportUITests(unittest.TestCase):
    setUp = fixture.ProfessionEditorTests.setUp
    destroy_root = fixture.ProfessionEditorTests.destroy_root
    descendants = fixture.ProfessionEditorTests.descendants

    def test_import_button_saves_refreshes_and_preserves_history(self):
        self.assertEqual(self.root.title(), 'EntropiaTracker')
        path = Path('import.json'); values = {'Skinning': 3333.5, 'Butchering': 2222, 'Intelligence': 99}
        path.write_text(json.dumps({'skills': values}), encoding='utf-8-sig')
        self.app.edit_profession_skill_value('Skinning', 'new', 1200)
        button = next(w for w in self.descendants(self.app.profession_tab) if isinstance(w, ttk.Button) and w.cget('text') == 'Import Skills JSON')
        with patch.object(tracker.filedialog, 'askopenfilename', return_value=str(path)), patch.object(tracker.messagebox, 'askyesno', return_value=True), patch.object(tracker.messagebox, 'showinfo'):
            button.invoke()
        self.assertEqual(self.app.current_skills, values); self.assertEqual(tracker.load_current_skills(), values)
        self.assertEqual(self.app.entries['Skinning']['current'], 3333.5); self.assertFalse(self.app.profession_skill_drafts)
        self.assertAlmostEqual(float(self.app.analysis_looter_vars['Animal'].get()), 14.08725, delta=0.000051)
        for path, content in self.sessions.items(): self.assertEqual(path.read_bytes(), content)
    def test_invalid_cancelled_or_unsavable_import_does_not_change_skills_or_drafts(self):
        path = Path('import.json'); self.app.edit_profession_skill_value('Skinning', 'new', 1200)
        original = copy.deepcopy(self.app.current_skills); drafts = copy.deepcopy(self.app.profession_skill_drafts)
        with patch.object(tracker.filedialog, 'askopenfilename', return_value=str(path)), patch.object(tracker.messagebox, 'showerror'), patch.object(tracker.messagebox, 'askyesno', return_value=True) as confirm:
            path.write_text('{"Skinning": -1}'); self.app.import_skills_json(); confirm.assert_not_called()
            path.write_text('{"Skinning": 3333}')
            with patch.object(tracker.messagebox, 'askyesno', return_value=False): self.app.import_skills_json()
            with patch.object(tracker, 'save_current_skills', side_effect=OSError('fixture write denied')): self.app.import_skills_json()
        self.assertEqual(self.app.current_skills, original); self.assertEqual(self.app.profession_skill_drafts, drafts)
        self.assertEqual(tracker.CURRENT_SKILLS_FILE.read_bytes(), self.saved)
