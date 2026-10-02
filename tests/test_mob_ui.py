import copy
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
import test_hunting_setup as fixture
import entropia_tracker_ui as tracker
from mob_catalog import load_mobs

class MobUITests(unittest.TestCase):
    setUp = fixture.HuntingSetupTests.setUp
    destroy_root = fixture.HuntingSetupTests.destroy_root

    def test_catalog_tab_editor_persists_maturity_and_refreshes_hunting_choices(self):
        before = copy.deepcopy(tracker.MOBS)
        self.addCleanup(lambda: (tracker.MOBS.clear(), tracker.MOBS.update(before)))
        self.assertIn(str(self.app.mob_catalog_tab), self.app.notebook.tabs())
        self.assertEqual(len(self.app.mob_catalog_tree.get_children()), 2)
        self.assertTrue(any(button.cget('text') == 'Sync mobs' for button in self.app.server_upload_buttons))
        self.app.edit_catalog_mob()
        window = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(window)); entries = [w for w in widgets if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox)]
        for field, value in zip(entries, ['Created mob', 'Arkadia', 'Young', '123', '7']): field.delete(0,'end'); field.insert(0,value)
        next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == 'Add / update maturity').invoke()
        next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == 'Save mob').invoke()
        self.root.update()
        self.assertEqual(tracker.MOBS['Created mob']['maturities']['Young']['hp'],123)
        self.assertIn('Created mob', self.app.all_mob_names)
        self.assertEqual(load_mobs({}, Path('mob_catalog.json'))['Created mob']['maturities']['Young']['level'],7)
