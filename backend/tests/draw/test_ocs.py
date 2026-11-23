"""Tests for the OpenCADStudio client (backend/ocre_core/draw/ocs.py).

The mock tests run against a fake in-process HTTP server that mimics
the real v2026.44 REST surface (shapes taken from the spike of
2026-11-05: flat ``audit``/``xdata_get`` bodies, ``result``-wrapped
``save_verified``/``entities_create``). No OCS binary is needed.

The real integration test at the bottom is skipped honestly when no
OpenCADStudio binary is present (``OCS_BINARY`` or PATH) — a skip is
not a pass.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ocre_core.draw.ocs import (
    APPID,
    BATCH_MAX_STEPS,
    OcsClient,
    OcsError,
    OcsUnavailableError,
    find_ocs_binary,
    launch_headless,
    stop_server,
)

TOKEN = "fake-test-token"

# Canned entities: 3 Draw strokes + 3 ezdxf-template arrow-block children.
ENTITIES = [
    {"handle": "77", "type": "Polyline", "layer": "0",
     "space": "_ARCHTICK", "vertices": [[-0.5, -0.5], [0.5, 0.5]]},
    {"handle": "7B", "type": "Solid", "layer": "0",
     "space": "_CLOSEDFILLED"},
    {"handle": "87", "type": "Polyline", "layer": "0",
     "space": "_CLOSEDBLANK",
     "vertices": [[-1.0, 0.16], [0.0, 0.0], [-1.0, -0.16]]},
    {"handle": "8C", "type": "Polyline", "layer": "DRAW_STROKES",
     "space": "*Model_Space",
     "vertices": [[10.0, 266.47], [90.0, 266.47], [90.0, 226.47], [10.0, 226.47]]},
    {"handle": "8D", "type": "Polyline", "layer": "DRAW_STROKES",
     "space": "*Model_Space",
     "vertices": [[120.0, 286.47], [150.0, 256.47], [180.0, 286.47]]},
    {"handle": "8E", "type": "Polyline", "layer": "DRAW_STROKES",
     "space": "*Model_Space",
     "vertices": [[10.0, 96.47], [100.0, 96.47]]},
]


class _FakeOcsHandler(BaseHTTPRequestHandler):
    """Minimal fake of the v2026.44 REST surface (state lives on the server)."""

    protocol_version = "HTTP/1.1"
    # Mutable fake state, reset per test class setup.
    xdata: dict[str, str] = {}
    idempotency: dict[str, dict] = {}
    points_created = 0
    lossy = False  # when True, audit/save_verified report dropped_on_save > 0
    always_stale = False
    deleted: set[str] = set()  # handles erased via entities_delete

    def log_message(self, *args):  # silence the test server
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _unauthorized(self):
        self._send(401, {"ok": False, "code": "unauthorized",
                         "error": "missing or wrong bearer token"})

    def do_GET(self):
        if not self._auth_ok():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/ready":
            self._send(200, {"ok": True, "ready": True, "version": "2026.44",
                             "document_id": 1, "session_id": "fake-session"})
        elif parsed.path == "/api/v1/state":
            self._send(200, {"ok": True, "document_id": 1, "revision": 5,
                             "geometry_revision": 3, "version": "2026.44"})
        elif parsed.path == "/api/v1/capabilities":
            self._send(200, {"ok": True, "operations": {
                "xdata_set": True, "xdata_get": True, "audit": True,
                "save_verified": True, "entities_create": True}})
        elif parsed.path == "/api/v1/entities":
            query = parse_qs(parsed.query)
            entities = [e for e in ENTITIES
                        if e["handle"] not in type(self).deleted]
            if "layer" in query:
                entities = [e for e in entities if e["layer"] == query["layer"][0]]
            self._send(200, {"ok": True, "count": len(entities),
                             "document_id": 1, "entities": entities})
        else:
            self._send(404, {"ok": False, "code": "unknown_route",
                             "error": f"No route GET {parsed.path}"})

    def do_POST(self):
        if not self._auth_ok():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/v1/documents":
            self._send(201, {"ok": True, "total": 6,
                             "by_type": {"Polyline": 5, "Solid": 1}})
        elif parsed.path == "/api/v1/xdata_get":
            items = []
            for handle in body.get("handles", []):
                value = type(self).xdata.get(handle.upper())
                items.append({"handle": handle,
                              "xdata": {APPID: [value]} if value else {}})
            self._send(200, {"ok": True, "items": items})
        elif parsed.path == "/api/v1/audit":
            dropped = 2 if type(self).lossy else 0
            self._send(200, {"ok": True, "status": "passed",
                             "manifest": {"total": 6,
                                        "by_layer": {"0": 3, "DRAW_STROKES": 3}},
                             "target": {"format": "dxf", "version": "AC1015",
                                      "dropped_on_save": dropped,
                                      "lossless": dropped == 0}})
        elif parsed.path == "/api/v1/save_verified":
            if type(self).lossy:
                self._send(200, {"ok": True, "result": {
                    "verified": False, "dropped_on_save": 2,
                    "sha256": None, "saved": body.get("path")}})
            else:
                self._send(200, {"ok": True, "result": {
                    "verified": True, "dropped_on_save": 0,
                    "sha256": "a" * 64, "saved": body.get("path"),
                    "manifest": {"total": 6}}})
        elif parsed.path == "/api/v1/entities_create":
            if type(self).always_stale or body.get("revision") == 999999:
                self._send(409, {"ok": False, "code": "stale_state",
                                 "error": "Refresh state before editing",
                                 "status": "failed"})
                return
            request_id = body.get("request_id")
            if request_id in type(self).idempotency:
                self._send(200, type(self).idempotency[request_id])
                return
            type(self).points_created += 1
            response = {"ok": True, "status": "completed",
                        "request_id": request_id,
                        "result": {"created": 1, "handles": ["99"],
                                   "layers_created": []}}
            type(self).idempotency[request_id] = response
            self._send(200, response)
        elif parsed.path == "/api/v1/entities_delete":
            # All-or-nothing, like the spec (§6.3): a missing handle
            # aborts the whole delete and names the absent ones.
            handles = [str(h).upper() for h in body.get("handles", [])]
            existing = {e["handle"] for e in ENTITIES
                        if e["handle"] not in type(self).deleted}
            missing = [h for h in handles if h not in existing]
            if missing:
                self._send(404, {"ok": False, "code": "entity_absent",
                                 "error": "entity absent: "
                                 + ", ".join(missing),
                                 "status": "failed"})
                return
            for handle in handles:
                type(self).deleted.add(handle)
                type(self).xdata.pop(handle, None)
            self._send(200, {"ok": True, "status": "completed",
                             "request_id": body.get("request_id"),
                             "result": {"erased": len(handles)}})
        elif parsed.path == "/api/v1/batch":
            self._send(400, {"ok": False, "code": "unknown_operation",
                             "error": "Unknown operation", "status": "failed"})
        else:
            self._send(404, {"ok": False, "code": "unknown_route",
                             "error": f"No route POST {parsed.path}"})

    def do_PUT(self):
        if not self._auth_ok():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        # /api/v1/entities/{handle}/xdata/{app}
        if len(parts) == 7 and parts[5] == "xdata":
            handle, app = parts[4], parts[6]
            body = self._read_json()
            value = next((item["value"] for item in body
                          if item.get("code") == 1000), None)
            if app == APPID and value is not None:
                type(self).xdata[handle.upper()] = str(value)
                self._send(200, {"ok": True, "status": "completed",
                                 "result": {"app": APPID, "updated": 1}})
                return
            self._send(400, {"ok": False, "code": "invalid_xdata",
                             "error": "bad xdata payload"})
            return
        self._send(404, {"ok": False, "code": "unknown_route",
                         "error": f"No route PUT {parsed.path}"})


class _FakeOcsServer:
    def __init__(self):
        _FakeOcsHandler.xdata = {}
        _FakeOcsHandler.idempotency = {}
        _FakeOcsHandler.points_created = 0
        _FakeOcsHandler.lossy = False
        _FakeOcsHandler.always_stale = False
        _FakeOcsHandler.deleted = set()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOcsHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class OcsClientMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _FakeOcsServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        _FakeOcsHandler.xdata = {}
        _FakeOcsHandler.idempotency = {}
        _FakeOcsHandler.points_created = 0
        _FakeOcsHandler.lossy = False
        _FakeOcsHandler.always_stale = False
        _FakeOcsHandler.deleted = set()
        self.client = OcsClient(port=self.server.port, token=TOKEN)

    def _synced_three(self):
        """Syncs s-0001/2/3 onto the fake's 3 stroke handles."""
        with tempfile.TemporaryDirectory() as tmp:
            self.client.sync_strokes(
                "/tmp/f0/document.dxf",
                ["s-0001", "s-0002", "s-0003"],
                out_path=Path(tmp) / "verified.dxf",
            )

    def test_import_and_construction_need_no_server(self):
        # Construction only resolves the token; no HTTP happens.
        client = OcsClient(port=1, token=TOKEN)
        self.assertEqual(client.base_url, "http://127.0.0.1:1/api/v1")
        self.assertNotIn(TOKEN, client.base_url)

    def test_missing_token_fails_closed(self):
        with self.assertRaises(OcsError) as ctx:
            OcsClient(port=self.server.port, token=None)
        self.assertEqual(ctx.exception.code, "no_token")

    def test_token_file_must_be_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text(TOKEN)
            path.chmod(0o644)
            with self.assertRaises(OcsError) as ctx:
                OcsClient(port=self.server.port, token_file=path)
            self.assertEqual(ctx.exception.code, "bad_token_file")
            path.chmod(0o600)
            client = OcsClient(port=self.server.port, token_file=path)
            self.assertTrue(client.ready()["ok"])

    def test_non_loopback_host_refused(self):
        with self.assertRaises(OcsError) as ctx:
            OcsClient(host="example.com", token=TOKEN)
        self.assertEqual(ctx.exception.code, "not_loopback")

    def test_server_down_is_unavailable_not_success(self):
        client = OcsClient(port=1, token=TOKEN, timeout_s=2)
        with self.assertRaises(OcsUnavailableError):
            client.ready()

    def test_wrong_token_is_unauthorized(self):
        client = OcsClient(port=self.server.port, token="wrong")
        with self.assertRaises(OcsError) as ctx:
            client.ready()
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_ready_state_capabilities(self):
        self.assertTrue(self.client.ready()["ok"])
        state = self.client.state()
        self.assertEqual(state["document_id"], 1)
        self.assertEqual(state["revision"], 5)
        ops = self.client.capabilities()["operations"]
        self.assertTrue(ops["xdata_set"] and ops["save_verified"])

    def test_stroke_handles_filter_out_block_definition_geometry(self):
        handles = self.client.stroke_handles()
        self.assertEqual(handles, ["8C", "8D", "8E"])

    def test_xdata_round_trip_under_draw_appid(self):
        for handle, stroke_id in zip(["8C", "8D", "8E"],
                                     ["s-0001", "s-0002", "s-0003"]):
            result = self.client.set_stroke_id(handle, stroke_id)
            self.assertEqual(result, {"app": "DRAW", "updated": 1})
            self.assertEqual(self.client.get_stroke_id(handle), stroke_id)
        self.assertIsNone(self.client.get_stroke_id("77"))

    def test_audit_reports_dropped_on_save_zero(self):
        audit = self.client.audit()
        self.assertEqual(audit["target"]["dropped_on_save"], 0)
        self.assertTrue(audit["target"]["lossless"])

    def test_save_verified_returns_hash_and_refuses_loss(self):
        saved = self.client.save_verified("/tmp/out/verified.dxf")
        self.assertTrue(saved["verified"])
        self.assertEqual(saved["sha256"], "a" * 64)
        self.assertEqual(saved["dropped_on_save"], 0)
        _FakeOcsHandler.lossy = True
        with self.assertRaises(OcsError) as ctx:
            self.client.save_verified("/tmp/out/verified.dxf")
        self.assertEqual(ctx.exception.code, "save_failed")
        # audit itself reports the loss (sync_strokes turns it into a refusal).
        self.assertEqual(
            self.client.audit()["target"]["dropped_on_save"], 2)

    def test_sync_strokes_end_to_end_against_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "verified.dxf"
            report = self.client.sync_strokes(
                "/tmp/f0/document.dxf",
                ["s-0001", "s-0002", "s-0003"],
                out_path=out,
            )
        self.assertEqual(report["handles"], ["8C", "8D", "8E"])
        self.assertEqual(report["xdata_round_trip"],
                         {"s-0001": "s-0001", "s-0002": "s-0002",
                          "s-0003": "s-0003"})
        self.assertEqual(report["dropped_on_save"], 0)
        self.assertTrue(report["save_verified"]["verified"])
        self.assertLessEqual(report["gate_g2_1_open_to_queryable_s"], 2.0)

    def test_sync_strokes_refuses_count_mismatch(self):
        with self.assertRaises(OcsError) as ctx:
            self.client.sync_strokes("/tmp/f0/document.dxf",
                                     ["s-0001"], out_path="/tmp/o.dxf")
        self.assertEqual(ctx.exception.code, "stroke_count_mismatch")

    # -- Borracha (erase_strokes / sync_document) ---------------------------

    def test_erase_strokes_removes_only_addressed_ids(self):
        self._synced_three()
        report = self.client.erase_strokes(["s-0002"])
        self.assertEqual(report["erased"], ["s-0002"])
        self.assertEqual(report["handles"], ["8D"])
        self.assertEqual(report["erased_count"], 1)
        self.assertTrue(report["verified_absent"])
        # The erased id is gone from the full XDATA scan; the others
        # keep their ids and handles.
        self.assertEqual(self.client.scan_stroke_ids(),
                         {"s-0001": "8C", "s-0003": "8E"})
        self.assertEqual(self.client.stroke_handles(), ["8C", "8E"])
        self.assertIsNone(self.client.get_stroke_id("8D"))

    def test_erase_strokes_removes_several_at_once(self):
        self._synced_three()
        report = self.client.erase_strokes(["s-0001", "s-0003"])
        self.assertEqual(report["erased"], ["s-0001", "s-0003"])
        self.assertEqual(self.client.scan_stroke_ids(), {"s-0002": "8D"})

    def test_erase_unknown_stroke_id_fails_closed_and_erases_nothing(self):
        self._synced_three()
        with self.assertRaises(OcsError) as ctx:
            self.client.erase_strokes(["s-9999"])
        self.assertEqual(ctx.exception.code, "unknown_stroke_id")
        # Mixed known + unknown: the known one must NOT be erased.
        with self.assertRaises(OcsError) as ctx:
            self.client.erase_strokes(["s-0001", "s-9999"])
        self.assertEqual(ctx.exception.code, "unknown_stroke_id")
        self.assertEqual(self.client.scan_stroke_ids(),
                         {"s-0001": "8C", "s-0002": "8D", "s-0003": "8E"})

    def test_erase_empty_list_fails_closed(self):
        self._synced_three()
        with self.assertRaises(OcsError) as ctx:
            self.client.erase_strokes([])
        self.assertEqual(ctx.exception.code, "empty_erase")
        self.assertEqual(len(self.client.scan_stroke_ids()), 3)

    def test_save_verified_still_passes_after_erase(self):
        self._synced_three()
        self.client.erase_strokes(["s-0002"])
        saved = self.client.save_verified("/tmp/out/verified.dxf")
        self.assertTrue(saved["verified"])
        self.assertEqual(saved["dropped_on_save"], 0)

    def test_sync_document_proves_erased_absent(self):
        self._synced_three()
        document = {"schema": "draw.paper.v1", "strokes": [
            {"stroke_id": "s-0001", "status": "stable"},
            {"stroke_id": "s-0002", "status": "erased"},
            {"stroke_id": "s-0003", "status": "stable"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "verified.dxf"
            report = self.client.sync_document(document, out_path=out)
        self.assertEqual(report["erased"], ["s-0002"])
        self.assertEqual(report["already_absent"], [])
        self.assertEqual(report["live"], ["s-0001", "s-0003"])
        self.assertEqual(report["remaining"],
                         {"s-0001": "8C", "s-0003": "8E"})
        self.assertTrue(report["verified_absent"])
        self.assertTrue(report["save_verified"]["verified"])
        self.assertEqual(report["save_verified"]["dropped_on_save"], 0)

    def test_sync_document_does_not_require_pending_strokes_in_cad(self):
        # Traço pending nunca vai para o DXF da F0 — exigi-lo no CAD era
        # stroke_missing_in_cad garantido e espúrio. Presente (aqui,
        # synced à mão), ele é mantido: o documento não o mandou apagar.
        self._synced_three()
        document = {"schema": "draw.paper.v1", "strokes": [
            {"stroke_id": "s-0001", "status": "stable"},
            {"stroke_id": "s-0002", "status": "erased"},
            {"stroke_id": "s-0003", "status": "pending"},
        ]}
        report = self.client.sync_document(document)
        self.assertEqual(report["erased"], ["s-0002"])
        self.assertEqual(report["live"], ["s-0001"])
        self.assertEqual(report["remaining"],
                         {"s-0001": "8C", "s-0003": "8E"})

    def test_sync_document_erased_already_absent_is_not_an_error(self):
        self._synced_three()
        self.client.erase_strokes(["s-0002"])
        document = {"schema": "draw.paper.v1", "strokes": [
            {"stroke_id": "s-0001", "status": "stable"},
            {"stroke_id": "s-0002", "status": "erased"},
            {"stroke_id": "s-0003", "status": "stable"},
        ]}
        report = self.client.sync_document(document)
        self.assertEqual(report["erased"], [])
        self.assertEqual(report["already_absent"], ["s-0002"])
        self.assertIsNone(report["save_verified"])

    def test_sync_document_fails_closed_on_cad_divergence(self):
        self._synced_three()
        # CAD has s-0003, the document does not know it.
        with self.assertRaises(OcsError) as ctx:
            self.client.sync_document({"schema": "draw.paper.v1",
                                       "strokes": [
                                           {"stroke_id": "s-0001",
                                            "status": "stable"},
                                           {"stroke_id": "s-0002",
                                            "status": "erased"},
                                       ]})
        self.assertEqual(ctx.exception.code, "unknown_stroke_in_cad")
        # The document has a live stroke the CAD does not have.
        with self.assertRaises(OcsError) as ctx:
            self.client.sync_document({"schema": "draw.paper.v1",
                                       "strokes": [
                                           {"stroke_id": "s-0001",
                                            "status": "stable"},
                                           {"stroke_id": "s-0002",
                                            "status": "stable"},
                                           {"stroke_id": "s-0003",
                                            "status": "stable"},
                                           {"stroke_id": "s-0004",
                                            "status": "stable"},
                                       ]})
        self.assertEqual(ctx.exception.code, "stroke_missing_in_cad")
        # Nothing was erased by either failed sync.
        self.assertEqual(len(self.client.scan_stroke_ids()), 3)

    def test_sync_document_rejects_malformed_document(self):
        with self.assertRaises(ValueError):
            self.client.sync_document({"schema": "draw.paper.v1"})
        with self.assertRaises(ValueError):
            self.client.sync_document({"schema": "draw.paper.v1",
                                       "strokes": [
                                           {"stroke_id": "s-0001",
                                            "status": "weird"},
                                       ]})

    def test_stale_state_retries_once_with_fresh_revision(self):
        # Same request_id, bogus revision first: 409, then retry succeeds.
        result = self.client._envelope(
            "entities_create",
            {"revision": 999999,
             "entities": [{"type": "Point", "location": [5, 5],
                           "layer": "DRAW_STROKES"}]},
            request_id="stale-retry-1",
        )
        self.assertEqual(result["created"], 1)
        self.assertEqual(_FakeOcsHandler.points_created, 1)

    def test_stale_state_twice_raises(self):
        _FakeOcsHandler.always_stale = True
        with self.assertRaises(OcsError) as ctx:
            self.client._envelope(
                "entities_create",
                {"entities": [{"type": "Point", "location": [5, 5],
                              "layer": "DRAW_STROKES"}]},
                request_id="stale-twice-1",
            )
        self.assertEqual(ctx.exception.code, "stale_state")

    def test_idempotent_replay_creates_one_entity_only(self):
        body = {"entities": [{"type": "Point", "location": [5, 5],
                              "layer": "DRAW_STROKES"}]}
        first = self.client._envelope("entities_create", body,
                                      request_id="idem-1")
        second = self.client._envelope("entities_create", body,
                                       request_id="idem-1")
        self.assertEqual(first, second)
        self.assertEqual(_FakeOcsHandler.points_created, 1)

    def test_run_steps_stops_at_first_failure_and_caps_at_64(self):
        with self.assertRaises(ValueError):
            self.client.run_steps([{"op": "entities_create"}]
                                  * (BATCH_MAX_STEPS + 1))
        result = self.client.run_steps([
            {"op": "entities_create", "request_id": "steps-1",
             "params": {"entities": [{"type": "Point", "location": [1, 1],
                                      "layer": "DRAW_STROKES"}]}},
            {"op": "entities_create", "request_id": "steps-2",
             "params": {"revision": 999999,
                        "entities": [{"type": "Point", "location": [2, 2],
                                      "layer": "DRAW_STROKES"}]}},
        ])
        # The second step is stale once, then retried with a fresh
        # revision and succeeds — so both complete here.
        self.assertEqual(result["completed_steps"], 2)


@unittest.skipUnless(find_ocs_binary(), "sem binário OpenCADStudio "
                     "(OCS_BINARY ou PATH) — spike real não rodado; "
                     "o AppImage v2026.44 do spike manual não está no PATH")
class OcsRealBinaryTests(unittest.TestCase):
    """Integração REAL contra o binário — só roda com o OCS instalado."""

    def test_real_server_xdata_round_trip_and_save_verified(self):
        from ocre_core.draw.dxf_export import export_dxf

        binary = find_ocs_binary()
        self.assertIsNotNone(binary)
        document = {
            "schema": "draw.paper.v1",
            "paper": {"format": "A4", "width_mm": 210.0, "height_mm": 297.0,
                      "effective_width_mm": 210.0,
                      "effective_height_mm": 296.47},
            "strokes": [
                {"stroke_id": "s-0001", "status": "stable", "closed": True,
                 "points_mm": [[10.0, 30.0], [90.0, 30.0], [90.0, 70.0],
                               [10.0, 70.0]]},
                {"stroke_id": "s-0002", "status": "stable", "closed": False,
                 "points_mm": [[120.0, 10.0], [150.0, 40.0], [180.0, 10.0]]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dxf_path = tmp_path / "document.dxf"
            export_dxf(document, dxf_path)
            out_path = tmp_path / "verified.dxf"
            process, client = launch_headless(
                binary, token="ocs-test-token-real")
            try:
                report = client.sync_strokes(
                    dxf_path, ["s-0001", "s-0002"], out_path=out_path)
            finally:
                stop_server(process)
            self.assertEqual(report["xdata_round_trip"],
                             {"s-0001": "s-0001", "s-0002": "s-0002"})
            self.assertEqual(report["dropped_on_save"], 0)
            self.assertTrue(report["save_verified"]["verified"])
            self.assertLessEqual(report["gate_g2_1_open_to_queryable_s"], 2.0)
            self.assertTrue(out_path.is_file())
            # O id sobrevive no arquivo, fora do OCS (ezdxf lê o DXF).
            import ezdxf
            saved = ezdxf.readfile(out_path)
            found = {e.get_xdata("DRAW")[0].value
                     for e in saved.modelspace()
                     if e.get_xdata("DRAW")}
            self.assertEqual(found, {"s-0001", "s-0002"})

    def test_real_server_erase_and_sync_document(self):
        """Borracha REAL: apaga no CAD via entities_delete e prova a
        ausência no arquivo salvo (ezdxf, fora do OCS)."""
        from ocre_core.draw import document as doc_mod
        from ocre_core.draw.dxf_export import export_dxf

        binary = find_ocs_binary()
        self.assertIsNotNone(binary)
        doc = doc_mod.new_document(
            {"repo": "GUZZBR1/ocre", "commit": "cad-finish-test",
             "origin": "fork-session-v2"})
        for points, closed in (
            ([[10.0, 30.0], [90.0, 30.0], [90.0, 70.0], [10.0, 70.0]], True),
            ([[120.0, 10.0], [150.0, 40.0], [180.0, 10.0]], False),
            ([[10.0, 100.0], [100.0, 100.0]], False),
        ):
            stroke = doc_mod.add_stroke(doc, points, closed=closed)
            doc_mod.stabilize_stroke(doc, stroke["stroke_id"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dxf_path = tmp_path / "document.dxf"
            export_dxf(doc, dxf_path)
            out_path = tmp_path / "verified.dxf"
            erased_path = tmp_path / "verified-after-erase.dxf"
            process, client = launch_headless(
                binary, token="ocs-test-token-real")
            try:
                client.sync_strokes(
                    dxf_path, ["s-0001", "s-0002", "s-0003"],
                    out_path=out_path)
                # A borracha no documento: tombstone em s-0002.
                doc_mod.erase_stroke(doc, "s-0002", reason="manual",
                                     at_ms=1000)
                report = client.sync_document(doc, out_path=erased_path)
            finally:
                stop_server(process)
            self.assertEqual(report["erased"], ["s-0002"])
            self.assertEqual(report["live"], ["s-0001", "s-0003"])
            self.assertTrue(report["verified_absent"])
            self.assertTrue(report["save_verified"]["verified"])
            self.assertEqual(report["save_verified"]["dropped_on_save"], 0)
            self.assertTrue(erased_path.is_file())
            # O apagado não está no arquivo salvo; os vivos estão.
            import ezdxf
            saved = ezdxf.readfile(erased_path)
            found = {e.get_xdata("DRAW")[0].value
                     for e in saved.modelspace()
                     if e.get_xdata("DRAW")}
            self.assertEqual(found, {"s-0001", "s-0003"})


if __name__ == "__main__":
    unittest.main()
