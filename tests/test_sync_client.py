"""Opt-in receipt sync (v0.5): config resolution, transport rules, idempotent push, CLI."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.contracts import RecordingSyncTransport
from openshard.history.shard_hash import compute_shard_hash
from openshard.sync import (
    HttpsSyncTransport,
    load_sync_state,
    push_entries,
    resolve_sync_config,
)
from openshard.sync.client import endpoint_allowed

FIXTURES = Path(__file__).parent / "fixtures" / "receipts" / "v2"


def _entries() -> list[dict]:
    out: list[dict] = []
    for name in ("01_verified_coding_task", "05_escalation_then_verified"):
        doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        out.extend(doc["entries"])
    return out


class TestConfig(unittest.TestCase):
    def test_off_by_default(self):
        cfg = resolve_sync_config({}, {})
        self.assertFalse(cfg.enabled)
        self.assertIsNone(cfg.endpoint)
        self.assertEqual(cfg.source, "none")

    def test_env_wins_over_config_and_needs_token(self):
        cfg = resolve_sync_config({"OPENSHARD_SYNC_ENDPOINT": "https://cloud.example/"},
                                  {"sync": {"endpoint": "https://other.example"}})
        self.assertEqual(cfg.endpoint, "https://cloud.example")
        self.assertFalse(cfg.enabled)
        self.assertIn("OPENSHARD_SYNC_TOKEN", cfg.reason)
        cfg = resolve_sync_config({"OPENSHARD_SYNC_ENDPOINT": "https://cloud.example",
                                  "OPENSHARD_SYNC_TOKEN": "t"}, {})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.source, "env")

    def test_config_endpoint(self):
        cfg = resolve_sync_config({"OPENSHARD_SYNC_TOKEN": "t"}, {"sync": {"endpoint": "https://c.example"}})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.source, "config")

    def test_plain_http_refused_except_loopback(self):
        self.assertFalse(endpoint_allowed("http://cloud.example"))
        self.assertTrue(endpoint_allowed("http://127.0.0.1:8000"))
        self.assertTrue(endpoint_allowed("http://localhost:8000"))
        self.assertFalse(endpoint_allowed("ftp://x"))
        self.assertFalse(endpoint_allowed(""))
        cfg = resolve_sync_config({"OPENSHARD_SYNC_ENDPOINT": "http://cloud.example", "OPENSHARD_SYNC_TOKEN": "t"}, {})
        self.assertFalse(cfg.enabled)
        with self.assertRaises(ValueError):
            HttpsSyncTransport("http://cloud.example", "t")

    def test_token_never_read_from_config(self):
        cfg = resolve_sync_config({"OPENSHARD_SYNC_ENDPOINT": "https://c.example"},
                                  {"sync": {"endpoint": "https://c.example", "token": "leaked"}})
        self.assertFalse(cfg.token_present)


class TestPush(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_push_is_idempotent_and_records_state(self):
        entries = _entries()
        with self.runner.isolated_filesystem():
            root = Path.cwd()
            t = RecordingSyncTransport()
            s1 = push_entries(entries, t, repo_path=root, endpoint="https://c.example")
            self.assertEqual((s1.considered, s1.sent, s1.failed), (3, 3, 0))
            self.assertEqual(len(t.envelopes), 3)
            state = load_sync_state(root)
            self.assertEqual(len(state["pushed"]), 3)
            self.assertEqual(state["endpoint"], "https://c.example")
            s2 = push_entries(entries, t, repo_path=root, endpoint="https://c.example")
            self.assertEqual((s2.sent, s2.skipped_unchanged), (0, 3))
            # A changed record (new hash) is re-sent; force re-sends all.
            changed = dict(entries[0], summary="edited")
            changed["content_hash"] = compute_shard_hash(changed)
            s3 = push_entries([changed] + entries[1:], t, repo_path=root, endpoint="https://c.example")
            self.assertEqual(s3.sent, 1)
            s4 = push_entries(entries, t, repo_path=root, endpoint="https://c.example", force=True)
            self.assertEqual(s4.sent, 3)

    def test_envelopes_carry_contract_with_siblings_and_outcomes(self):
        entries = _entries()
        t = RecordingSyncTransport()
        with self.runner.isolated_filesystem():
            push_entries(entries, t, repo_path=Path.cwd(), endpoint="https://c.example",
                         outcomes={"shard-20260914-0006": {"status": "merged", "source": "github"}})
        by_shard = {e["shard_id"]: e for e in t.envelopes}
        esc = by_shard["shard-20260914-0006"]
        self.assertEqual(esc["receipt_contract"]["state"], "VERIFIED_AFTER_ESCALATION")
        self.assertEqual(esc["receipt_contract"]["outcome"]["status"], "merged")
        self.assertEqual(esc["attempt_number"], 2)

    def test_failures_do_not_record_state(self):
        with self.runner.isolated_filesystem():
            root = Path.cwd()
            s = push_entries(_entries(), RecordingSyncTransport(fail=True), repo_path=root, endpoint="https://c.example")
            self.assertEqual(s.failed, 3)
            self.assertEqual(load_sync_state(root)["pushed"], {})

    def test_dry_run_sends_nothing(self):
        t = RecordingSyncTransport()
        with self.runner.isolated_filesystem():
            s = push_entries(_entries(), t, repo_path=Path.cwd(), endpoint="https://c.example", dry_run=True)
        self.assertEqual(t.envelopes, [])
        self.assertTrue(all(r["status"] == "would_send" for r in s.results))
        self.assertEqual(s.results[0]["state"], "VERIFIED")

    def test_garbage_entries_are_skipped_not_fatal(self):
        with self.runner.isolated_filesystem():
            s = push_entries([None, "x", {"task": "no ids"}], RecordingSyncTransport(),  # type: ignore[list-item]
                             repo_path=Path.cwd(), endpoint="https://c.example")
        self.assertEqual(s.considered, 1)
        self.assertEqual(s.sent, 1)


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    token = "secret-token"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_response(401)
            self.end_headers()
            return
        if self.path != "/api/v1/sync/receipts":
            self.send_response(404)
            self.end_headers()
            return
        type(self).received.append(body)
        payload = json.dumps({"id": f"rec_{len(self.received)}", "status": "created"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # silence
        return


class TestHttpsTransportLoopback(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.received = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_post_and_auth(self):
        from openshard.contracts import build_sync_envelope

        env = build_sync_envelope(_entries()[0])
        ok = HttpsSyncTransport(self.endpoint, "secret-token").push(env)
        self.assertTrue(ok.accepted)
        self.assertEqual(ok.status, "created")
        self.assertEqual(ok.remote_id, "rec_1")
        self.assertEqual(_Handler.received[0]["shard_id"], "shard-20260914-0001")
        bad = HttpsSyncTransport(self.endpoint, "wrong").push(env)
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.status, "unauthorized")

    def test_unreachable_is_categorised(self):
        from openshard.contracts import build_sync_envelope

        res = HttpsSyncTransport("http://127.0.0.1:9", "t", timeout=1.0).push(build_sync_envelope(_entries()[0]))
        self.assertFalse(res.accepted)
        self.assertEqual(res.status, "unreachable")
        self.assertEqual(res.error_category, "transport")

    def test_cli_push_end_to_end(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            with (Path(".openshard") / "runs.jsonl").open("w", encoding="utf-8") as fh:
                for e in _entries():
                    fh.write(json.dumps(e) + "\n")
            env = {"OPENSHARD_SYNC_ENDPOINT": self.endpoint, "OPENSHARD_SYNC_TOKEN": "secret-token"}
            status = runner.invoke(cli, ["sync", "status", "--json"], env=env)
            self.assertEqual(status.exit_code, 0, status.output)
            doc = json.loads(status.output)
            self.assertTrue(doc["enabled"])
            self.assertEqual(doc["receipts_pending"], 3)
            res = runner.invoke(cli, ["sync", "push", "--json"], env=env)
            self.assertEqual(res.exit_code, 0, res.output)
            data = json.loads(res.output)
            self.assertEqual(data["sent"], 3)
            again = runner.invoke(cli, ["sync", "push"], env=env)
            self.assertEqual(again.exit_code, 0, again.output)
            self.assertIn("3 unchanged", again.output)
            self.assertEqual(len(_Handler.received), 3)
            self.assertNotIn("secret-token", res.output + again.output + status.output)


class TestCliOffByDefault(unittest.TestCase):
    def test_push_refuses_without_config(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".openshard").mkdir()
            (Path(".openshard") / "runs.jsonl").write_text(json.dumps(_entries()[0]) + "\n", encoding="utf-8")
            res = runner.invoke(cli, ["sync", "push"], env={"OPENSHARD_SYNC_ENDPOINT": "", "OPENSHARD_SYNC_TOKEN": ""})
            self.assertEqual(res.exit_code, 2)
            self.assertIn("Sync is off", res.output)
            dry = runner.invoke(cli, ["sync", "push", "--dry-run", "--json"],
                                env={"OPENSHARD_SYNC_ENDPOINT": "", "OPENSHARD_SYNC_TOKEN": ""})
            self.assertEqual(dry.exit_code, 0, dry.output)
            self.assertEqual(json.loads(dry.output)["results"][0]["status"], "would_send")
            status = runner.invoke(cli, ["sync", "status"], env={"OPENSHARD_SYNC_ENDPOINT": "", "OPENSHARD_SYNC_TOKEN": ""})
            self.assertIn("Receipt sync: off", status.output)


if __name__ == "__main__":
    unittest.main()
