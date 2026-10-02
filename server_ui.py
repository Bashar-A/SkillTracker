"""Server settings and analytic-session uploads for the desktop tracker."""
import json
import os
import queue
import tempfile
import threading
import copy
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from server_sync import ServerClient, UploadError, fingerprint, item_rows, normalize_host, session_payload

SERVER_SETTINGS_FILE = Path("server_settings.json")
UPLOAD_RECEIPTS_FILE = Path("server_upload_receipts.json")


def read_local(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_private(path, value):
    """Atomic write without creating an extra backup containing the token."""
    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


from mob_ui import MobCatalogUI
from mob_catalog import save_mobs

class ServerUploadUI(MobCatalogUI):
    def initialize_server_uploads(self):
        config = read_local(SERVER_SETTINGS_FILE)
        self.server_host_var = tk.StringVar(value=config.get("host", ""))
        self.server_token_var = tk.StringVar(value=config.get("token", ""))
        self.server_remember_var = tk.BooleanVar(value=bool(config.get("token")))
        self.server_allow_http_var = tk.BooleanVar(value=bool(config.get("allow_http", False)))
        self.server_items_path_var = tk.StringVar(value=config.get("items_path", ""))
        self.server_status_var = tk.StringVar(value="Configure your server URL and API token to upload data.")
        self.analytic_status_var = tk.StringVar(value="")
        self.server_upload_buttons = []
        self.server_upload_queue = queue.Queue()
        self.server_upload_busy = False
        self.server_receipts = read_local(UPLOAD_RECEIPTS_FILE)
        self.server_signature_cache = {}
        self.server_receipt_warning = ""

    def server_button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        self.server_upload_buttons.append(button)
        return button

    def create_settings_tab(self):
        content = self.scrollable_tab_content(self.settings_tab)
        connection = ttk.LabelFrame(content, text="Mediocre Entropia server", padding=12)
        connection.pack(fill="x", padx=10, pady=10)
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Server URL:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(connection, textvariable=self.server_host_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(connection, text="API token:").grid(row=1, column=0, sticky="w", pady=5)
        token_entry = ttk.Entry(connection, textvariable=self.server_token_var, show="*")
        token_entry.grid(row=1, column=1, sticky="ew", padx=8)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(connection, text="Show", variable=show,
                        command=lambda: token_entry.configure(show="" if show.get() else "*")).grid(row=1, column=2)
        ttk.Checkbutton(connection, text="Remember token on this computer", variable=self.server_remember_var).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Checkbutton(connection, text="Allow HTTP on my trusted local network (token is sent unencrypted)",
                        variable=self.server_allow_http_var).grid(row=3, column=1, columnspan=2, sticky="w", padx=8)
        hint = ("Enter your running service URL, for example https://entropia.example.com or http://192.168.1.110:8088. "
                "Generate a token in your approved account on the server. A remembered token is stored locally without encryption.")
        ttk.Label(connection, text=hint, wraplength=690, justify="left").grid(row=4, column=0, columnspan=3, sticky="ew", pady=8)
        actions = ttk.Frame(connection)
        actions.grid(row=5, column=0, columnspan=3, sticky="w")
        self.server_button(actions, "Save settings", self.save_server_settings).pack(side="left", padx=(0, 8))
        self.server_button(actions, "Test connection", lambda: self.start_server_upload("test")).pack(side="left")
        self.server_button(actions, "Sync mobs", lambda: self.start_server_upload("mobs")).pack(side="left", padx=8)
        uploads = ttk.LabelFrame(content, text="Upload data", padding=12)
        uploads.pack(fill="x", padx=10, pady=10)
        uploads.columnconfigure(1, weight=1)
        ttk.Label(uploads, text="Items JSON file:").grid(row=0, column=0, sticky="w")
        ttk.Entry(uploads, textvariable=self.server_items_path_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(uploads, text="Browse...", command=self.choose_server_items_file).grid(row=0, column=2)
        upload_actions = ttk.Frame(uploads)
        upload_actions.grid(row=1, column=0, columnspan=3, sticky="ew", pady=6)
        for column in range(3):
            upload_actions.columnconfigure(column, weight=1, uniform="upload_actions")
        for index, (label, kind) in enumerate((("Upload analytic sessions", "sessions"), ("Upload current skills", "skills"),
                                               ("Upload items", "items"), ("Upload MU", "mu"), ("Upload items + MU", "items_mu"))):
            self.server_button(upload_actions, label, lambda kind=kind: self.start_server_upload(kind)).grid(row=index // 3, column=index % 3, sticky="ew", padx=3, pady=6)
        ttk.Label(uploads, text=("Skills replace your server skill snapshot. Items come from the selected JSON file. "
                  "MU uses manual overrides, then weekly market data (percentage or TT+ PED). "
                  "Unspecified server item fields are preserved; new items have zero TT fields until provided. "
                  "Session uploads require another member's approval before inclusion in server reports."),
                  wraplength=690, justify="left").grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Label(content, textvariable=self.server_status_var, wraplength=720, justify="left").pack(fill="x", padx=20, pady=10)

    def choose_server_items_file(self):
        path = filedialog.askopenfilename(title="Items JSON catalog", filetypes=[("JSON", "*.json")])
        if path:
            self.server_items_path_var.set(path)

    def save_server_settings(self):
        try:
            host = normalize_host(self.server_host_var.get(), self.server_allow_http_var.get())
            settings = {"host": host, "allow_http": self.server_allow_http_var.get(), "items_path": self.server_items_path_var.get()}
            if self.server_remember_var.get():
                settings["token"] = self.server_token_var.get().strip()
            write_private(SERVER_SETTINGS_FILE, settings)
        except (ValueError, OSError) as error:
            messagebox.showerror("Settings not saved", str(error))
            return False
        self.server_host_var.set(host)
        self.server_status_var.set("Server settings saved.")
        self.refresh_analytic_sessions()
        return True

    def create_analytic_sessions_tab(self, pager_class):
        actions = ttk.Frame(self.analytic_sessions_tab, padding=10)
        actions.pack(fill="x")
        ttk.Button(actions, text="Refresh", command=self.refresh_analytic_sessions).pack(side="left")
        self.server_button(actions, "Upload selected", lambda: self.start_server_upload("sessions", selected=True)).pack(side="left", padx=8)
        self.server_button(actions, "Upload all analytic sessions", lambda: self.start_server_upload("sessions")).pack(side="left")
        ttk.Button(actions, text="Server settings", command=lambda: self.notebook.select(self.settings_tab)).pack(side="left", padx=8)
        ttk.Label(self.analytic_sessions_tab, textvariable=self.analytic_status_var, wraplength=850, justify="left").pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(self.analytic_sessions_tab, textvariable=self.server_status_var, wraplength=850, justify="left").pack(fill="x", padx=10, pady=(0, 8))
        columns = (("started", "Started", 160), ("ended", "Ended", 160), ("mob", "Mob / Maturity", 200),
                   ("weapon", "Weapon", 220), ("ped", "PED cycled", 100), ("loot", "Loot PED", 100),
                   ("events", "Loot events", 90), ("upload", "Upload status", 270), ("notes", "Notes", 240))
        self.analytic_sessions_tree = ttk.Treeview(self.analytic_sessions_tab, columns=[row[0] for row in columns], show="headings", selectmode="extended")
        for key, title, width in columns:
            self.analytic_sessions_tree.heading(key, text=title)
            self.analytic_sessions_tree.column(key, width=width, anchor="w")
        self.make_tree_sortable(self.analytic_sessions_tree, {key: title for key, title, _ in columns})
        # Reuse the existing history pager; upload-all includes every page.
        self.analytic_sessions_pager = pager_class(self, self.analytic_sessions_tree, self.analytic_sessions_tab)
        self.analytic_sessions_pager.controls.pack(fill="x", padx=10, pady=(0, 6))
        self.pack_table(self.analytic_sessions_tree, self.analytic_sessions_tab, padx=10, pady=(0, 10))

    def active_upload_destination(self):
        try:
            return ServerClient(self.server_host_var.get(), self.server_token_var.get(), self.server_allow_http_var.get()).destination
        except UploadError:
            return ""

    def analytic_upload_status(self, session, destination):
        if not session.get("ended_at"):
            return "Complete session first"
        if not session.get("id") or not session.get("mob") or not session.get("ped_cycled"):
            return "Needs ID, mob and PED cycled"
        destination_receipts = self.server_receipts.get(destination, {})
        receipt = destination_receipts.get(str(session.get("id", ""))) if isinstance(destination_receipts, dict) else None
        if not isinstance(receipt, dict):
            return "Not uploaded"
        # Object references prevent id reuse; caches only cover current archives.
        signature = (id(session.get("loot_events")), len(session.get("loot_events") or []), tuple(session.get("attachments") or []),
                     tuple(str(session.get(k, "")) for k in ("ped_cycled", "mob", "maturity", "notes", "weapon", "amplifier", "started_at", "ended_at", "damage_total", "dpp", "effective_dpp")))
        cached = self.server_signature_cache.get(id(session))
        if cached is None or cached[0] is not session or cached[1] != signature:
            try:
                digest = fingerprint(session_payload(session))
            except UploadError:
                return "Needs correction before upload"
            self.server_signature_cache[id(session)] = (session, signature, digest)
        else:
            digest = cached[2]
        if digest != receipt.get("fingerprint"):
            return "Changed since upload (review on server)"
        return "Duplicate (server receipt)" if receipt.get("duplicate") else "Uploaded (await server review)"

    def refresh_analytic_sessions(self):
        if not hasattr(self, "analytic_sessions_pager"):
            return
        current = {id(row) for row in self.analysis_sessions}
        self.server_signature_cache = {key: value for key, value in self.server_signature_cache.items() if key in current}
        rows = [(f"analytic_{index}", row) for index, row in reversed(list(enumerate(self.analysis_sessions))) if isinstance(row, dict)]
        destination = self.active_upload_destination()
        def values(pair):
            _, row = pair
            return (row.get("started_at", ""), row.get("ended_at", ""), f"{row.get('mob', '')} {row.get('maturity', '')}".strip(),
                    row.get("weapon", ""), row.get("ped_cycled", 0), row.get("loot_ped_total", 0),
                    len(row.get("loot_events") or []), self.analytic_upload_status(row, destination), row.get("notes", ""))
        self.analytic_sessions_pager.set_rows(rows, values)
        self.analytic_status_var.set(f"{len(rows)} local analytic sessions. Add or remove entries through Previous Sessions. "
                                     "Select rows on the current page, or upload all pages. Receipts show submission, not moderation status.")

    def local_mu_rows(self):
        self.reload_market_data_if_changed()
        markups = {name: dict(value) for name, value in self.market_weekly_markups.items()}
        markups.update({name: {"type": "percentage", "value": value} for name, value in self.loot_markups.items()})
        return [{"name": name, "markupType": value["type"], "markup": max(100, value["value"]) if value["type"] == "percentage" else value["value"]}
                for name, value in sorted(markups.items())]

    def start_server_upload(self, kind, selected=False):
        if self.server_upload_busy:
            return
        try:
            client = ServerClient(self.server_host_var.get(), self.server_token_var.get(), self.server_allow_http_var.get())
            if kind == "sessions":
                if selected:
                    rows = [self.analytic_sessions_pager.display_records[iid] for iid in self.analytic_sessions_tree.selection()]
                else:
                    rows = list(self.analysis_sessions)
                if not rows:
                    raise UploadError("Select analytic sessions first." if selected else "Add sessions to Mob Analysis through Previous Sessions first.")
                # References are captured here; payload preparation runs off the UI thread.
                data = list(rows)
            elif kind == "mobs":
                data = copy.deepcopy(self.mob_catalog)
            elif kind == "skills":
                data = json.dumps(dict(self.current_skills), allow_nan=False)
            elif kind in ("items", "items_mu"):
                path = self.server_items_path_var.get().strip()
                if not path:
                    raise UploadError("Choose an items JSON file in Settings first.")
                data = path
            elif kind == "mu":
                data = json.dumps(self.local_mu_rows(), allow_nan=False)
            else:
                data = None
            mu = self.local_mu_rows() if kind == "items_mu" else []
        except (ValueError, OSError, KeyError) as error:
            messagebox.showerror("Upload not started", str(error))
            return
        self.server_upload_busy = True
        self.server_receipt_warning = ""
        for button in self.server_upload_buttons:
            button.configure(state="disabled")
        self.server_status_var.set("Testing server connection..." if kind == "test" else f"Uploading {kind.replace('_', ' + ')}...")
        destination = client.destination
        def worker():
            completed = 0
            def progress(index, total, body, response):
                nonlocal completed
                completed = index
                self.server_upload_queue.put(("batch", destination, kind, index, total, body, response))
            try:
                if kind == "test":
                    client.request("items?page=1")
                elif kind == "mobs":
                    def persist(catalog):
                        save_mobs(catalog)
                        self.server_upload_queue.put(("mobs", catalog))
                    client.sync_mobs(data, persist)
                else:
                    if kind in ("items", "items_mu"):
                        raw = json.loads(Path(data).read_text(encoding="utf-8-sig"))
                        rows = item_rows(raw)
                        if kind == "items_mu":
                            combined = {row["name"].upper(): row for row in rows}
                            for row in mu:
                                key = row["name"].upper()
                                combined[key] = {**combined.get(key, {}), **row}
                            rows = list(combined.values())
                    elif kind == "sessions":
                        # Snapshot only import fields, rather than copying raw chat and skills.
                        rows = [copy.deepcopy(session_payload(row)) for row in data]
                    else:
                        rows = json.loads(data)
                    client.upload("items" if kind in ("items", "items_mu", "mu") else kind, rows, progress)
                self.server_upload_queue.put(("done", kind, ""))
            except Exception as error:
                # Errors stay visible; successful earlier batches retain receipts.
                message = str(error).replace(client.token, "[redacted]")
                self.server_upload_queue.put(("done", kind, f"Failed after {completed} successful batch(es): {message}"))
        threading.Thread(target=worker, name="server-upload", daemon=True).start()
        self.root.after(100, self.poll_server_upload)

    def poll_server_upload(self):
        done = False
        while True:
            try:
                event = self.server_upload_queue.get_nowait()
            except queue.Empty:
                break
            if event[0] == "mobs":
                self.apply_mob_catalog(event[1])
                self.server_status_var.set("Mob catalog saved locally; server records take priority.")
            elif event[0] == "batch":
                _, destination, kind, index, total, body, response = event
                if kind == "sessions":
                    originals = {row["id"]: row for row in body["sessions"]}
                    receipts = self.server_receipts.setdefault(destination, {})
                    for row in response.get("rows", []):
                        source_id = row.get("sourceId")
                        if source_id in originals:
                            receipts[source_id] = {"id": row.get("id"), "duplicate": row.get("duplicate", False),
                                "fingerprint": fingerprint(originals[source_id]), "uploaded_at": datetime.now(timezone.utc).isoformat()}
                    try:
                        write_private(UPLOAD_RECEIPTS_FILE, self.server_receipts)
                    except OSError:
                        self.server_receipt_warning = " Local receipts could not be saved. Re-uploading safely checks duplicates."
                    self.refresh_analytic_sessions()
                self.server_status_var.set(f"{kind.replace('_', ' + ')}: batch {index}/{total} accepted by server.")
            else:
                _, kind, error = event
                done = True
                self.server_upload_busy = False
                for button in self.server_upload_buttons:
                    button.configure(state="normal")
                message = error or ("Connection and API token verified." if kind == "test" else "Mob sync complete. Server data takes priority; local-only mobs were uploaded." if kind == "mobs" else
                                   "Upload complete. Sessions require independent approval on the server." if kind == "sessions" else "Upload complete.")
                self.server_status_var.set(message + self.server_receipt_warning)
                if error:
                    messagebox.showerror("Server upload failed", error)
        if not done:
            self.root.after(100, self.poll_server_upload)

