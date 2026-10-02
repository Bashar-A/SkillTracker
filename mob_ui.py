"""Desktop mob browser/editor using the shared local catalog."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox
from mob_catalog import merge_mobs, mob_rows, save_mobs

class MobCatalogUI:
    def create_mob_catalog_tab(self):
        toolbar = ttk.Frame(self.mob_catalog_tab, padding=10)
        toolbar.pack(fill='x')
        self.mob_catalog_search = tk.StringVar()
        ttk.Label(toolbar, text='Search mobs:').pack(side='left')
        ttk.Entry(toolbar, textvariable=self.mob_catalog_search).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(toolbar, text='Add mob', command=lambda: self.edit_catalog_mob()).pack(side='left')
        ttk.Button(toolbar, text='Edit selected', command=self.edit_selected_catalog_mob).pack(side='left', padx=8)
        ttk.Label(self.mob_catalog_tab, text='Double-click a mob to edit its type, planets, maturity HP and level. Sync with the server from Settings.').pack(anchor='w', padx=10)
        container = ttk.Frame(self.mob_catalog_tab, padding=10)
        container.pack(fill='both', expand=True)
        self.mob_catalog_tree = ttk.Treeview(container, columns=('name', 'type', 'planets', 'maturities'), show='headings', selectmode='browse')
        for key in ('name', 'type', 'planets', 'maturities'):
            self.mob_catalog_tree.heading(key, text=key.title())
            self.mob_catalog_tree.column(key, width=220 if key == 'name' else 120)
        scroll = ttk.Scrollbar(container, orient='vertical', command=self.mob_catalog_tree.yview)
        self.mob_catalog_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.mob_catalog_tree.pack(fill='both', expand=True)
        self.mob_catalog_tree.bind('<Double-1>', lambda _: self.edit_selected_catalog_mob())
        self.mob_catalog_search.trace_add('write', lambda *_: self.refresh_mob_catalog())
        self.refresh_mob_catalog()

    def refresh_mob_catalog(self):
        if not hasattr(self, 'mob_catalog_tree'): return
        self.mob_catalog_tree.delete(*self.mob_catalog_tree.get_children())
        self.mob_catalog_names = {}
        query = self.mob_catalog_search.get().casefold()
        for name in sorted(self.mob_catalog, key=str.casefold):
            row = self.mob_catalog[name]
            if query not in name.casefold(): continue
            iid = self.mob_catalog_tree.insert('', 'end', values=(name, row.get('type') or 'Unknown', ', '.join(row.get('planets', [])), len(row.get('maturities', {}))))
            self.mob_catalog_names[iid] = name

    def apply_mob_catalog(self, catalog):
        self.mob_catalog.clear(); self.mob_catalog.update(catalog)
        self.all_mob_names = sorted(catalog, key=str.casefold)
        self.mob_combo.configure(values=self.all_mob_names)
        self.update_maturity_values(); self.refresh_hunting_info(); self.refresh_favorite_mobs()
        self.refresh_mob_catalog()
        for entry in list(self.derived_cache().entries.values()): self.derived_cache().invalidate(entry['source'])

    def edit_selected_catalog_mob(self):
        selected = self.mob_catalog_tree.selection()
        if selected: self.edit_catalog_mob(self.mob_catalog_names[selected[0]])

    def edit_catalog_mob(self, original=None):
        if self.server_upload_busy:
            messagebox.showinfo('Sync in progress', 'Wait for the current server operation to finish before editing mobs.')
            return
        draft = copy.deepcopy(self.mob_catalog.get(original, {'type': 'Unknown', 'planets': [], 'maturities': {}}))
        window = tk.Toplevel(self.root); window.title('Edit mob' if original else 'Add mob'); window.geometry('760x600')
        form = ttk.Frame(window, padding=12); form.pack(fill='both', expand=True)
        name = tk.StringVar(value=original or ''); kind = tk.StringVar(value=draft.get('type') or 'Unknown'); planets = tk.StringVar(value=', '.join(draft.get('planets', [])))
        for label, variable in [('Mob name', name), ('Planets (comma separated)', planets)]:
            ttk.Label(form, text=label).pack(anchor='w'); ttk.Entry(form, textvariable=variable).pack(fill='x', pady=4)
        ttk.Label(form, text='Type').pack(anchor='w'); ttk.Combobox(form, textvariable=kind, values=('Animal', 'Robot', 'Mutant', 'Asteroid', 'Unknown'), state='readonly').pack(fill='x')
        tree = ttk.Treeview(form, columns=('name', 'hp', 'level'), show='headings', height=8)
        for key in ('name', 'hp', 'level'): tree.heading(key, text=key.title())
        scroll = ttk.Scrollbar(form, orient='vertical', command=tree.yview); tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill='both', expand=True, pady=8); scroll.pack(side='right', fill='y')
        maturity, hp, level = tk.StringVar(), tk.StringVar(), tk.StringVar()
        fields = ttk.Frame(form); fields.pack(fill='x')
        for label, variable in [('Maturity', maturity), ('HP (blank = unknown)', hp), ('Level (blank = unknown)', level)]:
            cell = ttk.Frame(fields); cell.pack(side='left', fill='x', expand=True)
            ttk.Label(cell, text=label).pack(anchor='w'); ttk.Entry(cell, textvariable=variable, width=20).pack(fill='x', padx=(0, 6))
        def refresh():
            tree.delete(*tree.get_children())
            for key, value in draft['maturities'].items(): tree.insert('', 'end', iid=key, values=(key, value.get('hp'), value.get('level')))
        def choose(_):
            if tree.selection():
                key = tree.selection()[0]; value = draft['maturities'][key]
                maturity.set(key); hp.set('' if value['hp'] is None else str(value['hp'])); level.set('' if value['level'] is None else str(value['level']))
        tree.bind('<<TreeviewSelect>>', choose)
        def put_maturity():
            try:
                candidate = {'name': name.get() or 'Draft', 'type': kind.get(), 'planets': [], 'maturities': {maturity.get(): {'hp': None if not hp.get().strip() else hp.get(), 'level': None if not level.get().strip() else level.get()}}}
                row = mob_rows([candidate])[0]
                # Case-insensitive maturity identity also applies to local editing.
                target = next(iter(row['maturities']))
                for key in list(draft['maturities']):
                    if key.upper() == target.upper(): del draft['maturities'][key]
                draft['maturities'].update(row['maturities']); refresh()
            except ValueError as error: messagebox.showerror('Invalid maturity', str(error), parent=window)
        def remove():
            for key in tree.selection(): draft['maturities'].pop(key, None)
            refresh()
        def save():
            try:
                if self.server_upload_busy: raise ValueError('Wait for server sync to finish.')
                row = mob_rows([dict(draft, name=name.get(), type=kind.get(), planets=[p.strip() for p in planets.get().split(',') if p.strip()])])[0]
                if any(key.upper() == row['name'].upper() and key != original for key in self.mob_catalog): raise ValueError('Mob already exists. Edit that entry instead.')
                updated = copy.deepcopy(self.mob_catalog)
                if original: updated.pop(original)
                updated = merge_mobs(updated, [row]); save_mobs(updated); self.apply_mob_catalog(updated); window.destroy()
            except (ValueError, OSError) as error: messagebox.showerror('Mob not saved', str(error), parent=window)
        actions = ttk.Frame(form); actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='Add / update maturity', command=put_maturity).pack(side='left')
        ttk.Button(actions, text='Remove maturity', command=remove).pack(side='left', padx=8)
        ttk.Button(actions, text='Save mob', command=save).pack(side='right')
        ttk.Label(form, text='Use Add / update maturity to apply HP and level edits, then Save mob.').pack(anchor='w')
        refresh(); self.style_tracker_widgets(window)
