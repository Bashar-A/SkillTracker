"""Protocol, privacy, partial-failure, and desktop upload regressions."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_sync as sync
import server_ui
import skill_tracker_ui as tracker
import test_hunting_setup as fixture


def session(index=0):
    return {"id": f"session-{index}", "mob": "Carabok", "started_at": "2026-10-01T10:00:00+03:00",
            "ended_at": "2026-10-01T11:00:00+03:00", "ped_cycled": 10, "loot_ped_total": 9,
            "chat_log_path": "C:/private/chat.log", "current_skills_at_end": {"Secret": 100},
            "events": [{"message": "private chat"}], "loot_events_before_exclusion": [{"secret": 1}],
            "loot_events": [{"value_ped": 9, "cost_ped": 10, "items": {"Shrapnel": 90000},
                             "messages": ["You received Shrapnel Value: 9 PED"], "excluded_loot_items": ["Hide"]}]}


class ProtocolTests(unittest.TestCase):
    def test_host_normalization_and_explicit_lan_http(self):
        self.assertEqual(sync.normalize_host(" https://example.test/api/ "), "https://example.test")
        self.assertEqual(sync.normalize_host("http://127.0.0.1:8088"), "http://127.0.0.1:8088")
        self.assertEqual(sync.normalize_host("http://192.168.1.110:8088", True), "http://192.168.1.110:8088")
        for url in ("http://192.168.1.110:8088", "https://u:p@example.test", "https://example.test/foo", "https://example.test/?token=abc", "file:///tmp/a"):
            with self.assertRaises(sync.UploadError):
                sync.normalize_host(url)

    def test_payload_retains_exclusions_and_offsets_without_local_private_data(self):
        raw = session()
        before = copy.deepcopy(raw)
        payload = sync.session_payload(raw)
        self.assertEqual(payload["started_at"], "2026-10-01T07:00:00+00:00")
        self.assertEqual(payload["loot_events"][0]["excluded_loot_items"], ["Hide"])
        self.assertEqual(payload["loot_events"][0]["messages"], raw["loot_events"][0]["messages"])
        for key in ("events", "chat_log_path", "current_skills_at_end", "loot_events_before_exclusion"):
            self.assertNotIn(key, payload)
        self.assertEqual(raw, before)
        raw["loot_events"][0]["excluded_from_loot"] = True
        self.assertTrue(sync.session_payload(raw)["loot_events"][0]["excluded_from_loot"])

    @unittest.skipUnless(hasattr(time, "tzset"), "Requires timezone control")
    def test_local_session_clock_is_converted_to_utc(self):
        previous = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Moscow"
            time.tzset()
            raw = session()
            raw["started_at"] = "2026-10-01T10:00:00"
            self.assertEqual(sync.session_payload(raw)["started_at"], "2026-10-01T07:00:00+00:00")
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def test_validation_happens_before_upload_requests(self):
        client = sync.ServerClient("https://example.test", "sth_test")
        for fields in ({"ended_at": None}, {"ped_cycled": 0}, {"mob": ""}, {"ped_cycled": float("nan")}, {"loot_events": None}):
            with patch.object(client, "request") as request:
                with self.assertRaises(sync.UploadError):
                    client.upload("sessions", [session(), {**session(1), **fields}])
                request.assert_not_called()
        with patch.object(client, "request") as request:
            with self.assertRaises(sync.UploadError):
                client.upload("skills", {})
            request.assert_not_called()

    def test_batches_obey_rows_events_and_bytes(self):
        self.assertEqual([len(b["sessions"]) for b in sync.batches("sessions", [session(i) for i in range(205)])], [100, 100, 5])
        with patch.object(sync, "MAX_EVENTS", 3):
            rows = [{"loot_events": [{}, {}]} for _ in range(3)]
            self.assertEqual(len(sync.batches("sessions", rows)), 3)
        with patch.object(sync, "MAX_BYTES", 140):
            self.assertEqual(len(sync.batches("items", [{"name": "a" * 60}, {"name": "b" * 60}, {"name": "c" * 60}])), 3)
            with self.assertRaises(sync.UploadError):
                sync.batches("items", [{"name": "a" * 160}])

    def test_current_versions_preserve_tt_fields_and_fixed_mu(self):
        old = {"name": "Gun", "value": 5, "minTt": 1, "maxTt": 40, "markupType": "fixed", "markup": 10, "version": 4}
        merged = sync.merge_items([{"name": "gun", "markupType": "fixed", "markup": 15}], {"GUN": old})[0]
        self.assertEqual((merged["name"], merged["value"], merged["maxTt"], merged["version"], merged["markup"]), ("Gun", 5, 40, 4, 15))
        item_only = sync.merge_items([{"name": "Gun", "value": 6}], {"GUN": old})[0]
        self.assertEqual((item_only["markupType"], item_only["markup"]), ("fixed", 10))
        self.assertEqual(old["markup"], 10)

    def test_item_import_reads_catalog_then_posts_current_versions(self):
        client = sync.ServerClient("https://example.test", "sth_test")
        old = {"name": "Hide", "value": .01, "minTt": 0, "maxTt": 0, "markupType": "percentage", "markup": 100, "version": 3}
        with patch.object(client, "request", side_effect=[{"total": 1, "rows": [old]}, {"count": 1}]) as request:
            client.upload("items", [{"name": "Hide", "markup": 105}])
        self.assertEqual(request.call_args_list[0].args, ("items?page=1",))
        self.assertEqual(request.call_args_list[1].args[1]["items"][0]["version"], 3)
        self.assertEqual(request.call_args_list[1].args[1]["items"][0]["value"], .01)

    def test_partial_failure_reports_only_successful_batches(self):
        client = sync.ServerClient("https://example.test", "sth_test")
        progress = []
        receipts = [{"sourceId": f"session-{i}", "id": f"remote-{i}", "duplicate": False} for i in range(100)]
        with patch.object(client, "request", side_effect=[{"rows": receipts}, sync.UploadError("Conflict")]):
            with self.assertRaises(sync.UploadError):
                client.upload("sessions", [session(i) for i in range(101)], lambda *args: progress.append(args))
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0][:2], (1, 2))

    def test_redirect_does_not_forward_token_and_http_error_redacts_secret(self):
        with self.assertRaises(sync.UploadError):
            sync.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test")
        client = sync.ServerClient("https://example.test", "sth_test")
        import io
        error = HTTPError("https://example.test", 409, "conflict", {}, io.BytesIO(b'{"error":"sth_test must not leak"}'))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(sync.UploadError) as result:
                client.request("items")
        self.assertNotIn("sth_test", str(result.exception))
        self.assertIn("409", str(result.exception))

    def test_private_file_is_replaced_without_secret_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            server_ui.write_private(path, {"token": "old"})
            server_ui.write_private(path, {"host": "https://example.test"})
            self.assertNotIn("token", server_ui.read_local(path))
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["settings.json"])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append((self.command, self.path, self.headers.get("Authorization"), None))
                if self.path.startswith("/api/uploads/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-token")
                    self.end_headers()
                else:
                    self.respond({"total": 0, "rows": []})
            def do_POST(self):
                self.write_body()
            def do_PUT(self):
                self.write_body()
            def write_body(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.command, self.path, self.headers.get("Authorization"), body))
                if "sessions" in body:
                    self.respond({"rows": [{"sourceId": row["id"], "id": "remote", "duplicate": False} for row in body["sessions"]]})
                elif "skills" in body:
                    self.respond({"skills": body["skills"]})
                else:
                    self.respond({"count": len(body["items"])})
            def respond(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = sync.ServerClient(f"http://127.0.0.1:{self.server.server_port}", "sth_transport_test")
        # Loopback tests must not depend on a developer's outbound proxy settings.
        from urllib.request import ProxyHandler, build_opener
        self.client.opener = build_opener(ProxyHandler({}), sync.NoRedirect())

    def test_real_json_requests_use_expected_methods_paths_and_bearer_header(self):
        self.client.upload("sessions", [session()])
        self.client.upload("skills", {"Anatomy": 100.5})
        self.client.upload("items", [{"name": "Hide", "value": .01, "markup": 105}])
        self.assertEqual([(row[0], row[1]) for row in self.calls], [
            ("POST", "/api/uploads/sessions"), ("PUT", "/api/uploads/skills"),
            ("GET", "/api/uploads/items?page=1"), ("POST", "/api/uploads/items")])
        self.assertTrue(all(row[2] == "Bearer sth_transport_test" for row in self.calls))
        self.assertNotIn("chat_log_path", self.calls[0][3]["sessions"][0])

    def test_real_redirect_is_rejected_without_a_second_request(self):
        with self.assertRaises(sync.UploadError) as error:
            self.client.request("redirect")
        self.assertIn("redirected", str(error.exception))
        self.assertEqual(len(self.calls), 1)


class UploadTkTests(unittest.TestCase):
    setUp = fixture.HuntingSetupTests.setUp
    destroy_root = fixture.HuntingSetupTests.destroy_root

    def configure(self):
        self.app.server_host_var.set("https://example.test")
        self.app.server_token_var.set("sth_test")

    def wait_upload(self):
        deadline = time.monotonic() + 5
        while self.app.server_upload_busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.app.server_upload_busy)

    def test_settings_opt_in_token_storage_and_no_session_file_changes(self):
        self.configure()
        self.assertTrue(self.app.save_server_settings())
        self.assertNotIn("token", server_ui.read_local(server_ui.SERVER_SETTINGS_FILE))
        self.app.server_remember_var.set(True)
        self.app.save_server_settings()
        self.assertEqual(server_ui.read_local(server_ui.SERVER_SETTINGS_FILE)["token"], "sth_test")
        self.app.server_remember_var.set(False)
        self.app.save_server_settings()
        self.assertNotIn("token", server_ui.read_local(server_ui.SERVER_SETTINGS_FILE))
        for path in (tracker.SESSIONS_FILE, tracker.ANALYSIS_SESSIONS_FILE):
            self.assertEqual(path.read_bytes(), self.original_files[path])

    def test_selected_upload_preserves_archive_and_persists_receipts(self):
        self.configure()
        self.app.analysis_sessions = [session(i) for i in range(3)]
        before = copy.deepcopy(self.app.analysis_sessions)
        self.app.notebook.select(self.app.analytic_sessions_tab)
        self.root.update()
        self.app.analytic_sessions_tree.selection_set("analytic_1")
        def request(client, path, body=None, method="GET"):
            self.assertEqual((path, method), ("sessions", "POST"))
            self.assertEqual([row["id"] for row in body["sessions"]], ["session-1"])
            return {"rows": [{"sourceId": "session-1", "id": "remote-id", "duplicate": False}]}
        with patch.object(sync.ServerClient, "request", new=request):
            self.app.start_server_upload("sessions", selected=True)
            self.wait_upload()
        self.assertEqual(self.app.analysis_sessions, before)
        self.assertIn("Uploaded", self.app.analytic_sessions_tree.set("analytic_1", "upload"))
        self.assertNotIn("sth_test", server_ui.UPLOAD_RECEIPTS_FILE.read_text())
        self.app.analysis_sessions[1]["ped_cycled"] = 11
        self.app.refresh_analytic_sessions()
        self.assertIn("Changed", self.app.analytic_sessions_tree.set("analytic_1", "upload"))

    def test_all_pages_upload_and_duplicates_appear_in_table(self):
        self.configure()
        self.app.analysis_sessions = [session(i) for i in range(205)]
        self.app.refresh_analytic_sessions()
        count = []
        def request(client, path, body=None, method="GET"):
            count.extend(row["id"] for row in body["sessions"])
            return {"rows": [{"sourceId": row["id"], "id": "remote-" + row["id"], "duplicate": True} for row in body["sessions"]]}
        with patch.object(sync.ServerClient, "request", new=request):
            self.app.start_server_upload("sessions")
            self.wait_upload()
        self.assertEqual(len(set(count)), 205)
        self.assertIn("Duplicate", self.app.analytic_sessions_tree.set("analytic_204", "upload"))
        self.app.analytic_sessions_pager.controls.go(1)
        self.assertIn("analytic_0", self.app.analytic_sessions_tree.get_children())

    def test_mu_manual_overrides_weekly_and_fixed_ped_remains_fixed(self):
        self.app.market_weekly_markups = {"Hide": {"type": "percentage", "value": 120}, "Gun": {"type": "fixed", "value": 15}}
        self.app.loot_markups = {"Hide": 130}
        with patch.object(self.app, "reload_market_data_if_changed"):
            rows = {row["name"]: row for row in self.app.local_mu_rows()}
        self.assertEqual(rows["Hide"]["markup"], 130)
        self.assertEqual((rows["Gun"]["markupType"], rows["Gun"]["markup"]), ("fixed", 15))

    def test_failed_later_batch_keeps_receipts_and_reenables_buttons(self):
        self.configure()
        self.app.analysis_sessions = [session(i) for i in range(101)]
        response = {"rows": [{"sourceId": f"session-{i}", "id": f"remote-{i}", "duplicate": False} for i in range(100)]}
        with patch.object(sync.ServerClient, "request", side_effect=[response, sync.UploadError("Conflict")]), patch.object(server_ui.messagebox, "showerror"):
            self.app.start_server_upload("sessions")
            self.wait_upload()
        self.assertIn("Failed after 1", self.app.server_status_var.get())
        self.assertEqual(len(self.app.server_receipts[self.app.active_upload_destination()]), 100)
        self.assertTrue(all(str(button.cget("state")) == "normal" for button in self.app.server_upload_buttons))

    def test_network_wait_does_not_block_tk_event_loop(self):
        self.configure()
        self.app.current_skills = {"Anatomy": 100}
        release = threading.Event()
        self.addCleanup(release.set)
        seen = []
        def request(*args, **kwargs):
            release.wait(3)
            return {"skills": {"Anatomy": 100}}
        with patch.object(sync.ServerClient, "request", side_effect=request):
            self.app.start_server_upload("skills")
            self.root.after(20, lambda: seen.append(True))
            deadline = time.monotonic() + 1
            while not seen and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertTrue(seen)
            self.assertTrue(self.app.server_upload_busy)
            release.set()
            self.wait_upload()


if __name__ == "__main__":
    unittest.main()
