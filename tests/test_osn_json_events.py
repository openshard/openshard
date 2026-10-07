"""`osn run --json-events`: the engine's progress events as NDJSON, then the result, nothing else on stdout.

One execution engine, two renderers: the terminal renderer for humans and
this stream for programs. Both consume the same ``progress`` callback, so a
streaming change can never diverge from what the terminal shows. The stream
never prompts (policy 'ask' paths are refused as under ``--json``).
"""
from __future__ import annotations

import json
import subprocess
import sys

from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn import checkpoint as ckpt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
CHECK = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        content = self.replies.pop(0)
        if isinstance(content, BaseException):
            raise content
        return ChatResponse(content, model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "out.txt").write_text("bad")
    return repo


def _turn(*actions):
    return json.dumps({"actions": list(actions), "note": "doing it"})


def _write(path, content):
    return {"kind": "write_file", "path": path, "content": content, "intent": "write"}


def _finish():
    return {"kind": "finish", "intent": "done"}


def _invoke(monkeypatch, repo, fake, *args):
    monkeypatch.chdir(repo)
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", "make out ok", "--model", "fake/m", "--verify-cmd", CHECK,
                                    "--roles", "executor", "--no-learning", *args])


def _lines(output: str) -> list[dict]:
    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    return rows


def test_every_stdout_line_is_an_event_and_the_last_is_the_result(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _invoke(monkeypatch, repo, FakeProvider([_turn(_write("out.txt", "ok"), _finish())]), "--json-events")
    assert r.exit_code == 0, r.output
    rows = _lines(r.output)  # raises if any line is not JSON
    names = [row["event"] for row in rows]
    assert names[0] == "workspace_ready" and names[-1] == "result"
    assert "turn_start" in names and "action" in names and "verification_result" in names
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(isinstance(row["elapsed_s"], float) and row["elapsed_s"] >= 0 for row in rows)
    assert all(set(row) == {"event", "seq", "elapsed_s", "data"} for row in rows)
    action = next(row for row in rows if row["event"] == "action" and row["data"].get("kind") == "write_file")
    assert action["data"]["target"] == "out.txt"
    verification = next(row for row in rows if row["event"] == "verification_result")
    assert verification["data"]["status"] == "passed"
    result = rows[-1]["data"]
    assert result["status"] == "verified" and result["changed_files"] == ["out.txt"]
    assert result["receipt_id"].startswith("rcpt_") and result["checkpoint"]["status"] == "completed"
    # The result object is the same one --json prints.
    plain = _invoke(monkeypatch, _repo(tmp_path / "b"), FakeProvider([_turn(_write("out.txt", "ok"), _finish())]),
                    "--json")
    assert set(json.loads(plain.output)) == set(result)


def test_a_failing_check_streams_why_and_no_prose(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _invoke(monkeypatch, repo, FakeProvider([_turn(_write("out.txt", "nope"), _finish())]),
                "--max-attempts", "1", "--json-events")
    assert r.exit_code == 0, r.output
    rows = _lines(r.output)
    failed = [row for row in rows if row["event"] == "verification_result"]
    assert failed and failed[-1]["data"]["status"] == "failed" and failed[-1]["data"]["exit_code"] == 1
    assert rows[-1]["event"] == "result" and rows[-1]["data"]["status"] == "failed"
    assert "Apply later" not in r.output and "OSN loop:" not in r.output


def test_the_stream_never_prompts(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fake = FakeProvider([_turn(_write("pyproject.toml", "x"), _write("out.txt", "ok"), _finish())])
    r = _invoke(monkeypatch, repo, fake, "--json-events")
    assert r.exit_code == 0, r.output
    rows = _lines(r.output)
    refused = [row for row in rows if row["event"] == "action" and row["data"].get("status") == "refused"]
    assert refused and refused[0]["data"]["target"] == "pyproject.toml"
    assert rows[-1]["data"]["status"] == "blocked"
    assert not (repo / "pyproject.toml").exists()


def test_an_interrupted_stream_ends_with_an_interrupted_event_and_resume_streams_too(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fake = FakeProvider([_turn(_write("out.txt", "wrong"), _finish()), KeyboardInterrupt()])
    r = _invoke(monkeypatch, repo, fake, "--escalate-model", "b/m", "--json-events")
    assert r.exit_code != 0
    rows = _lines("\n".join(line for line in r.output.splitlines() if line.startswith("{")))
    assert rows[-1]["event"] == "interrupted"
    run_id = rows[-1]["data"]["run_id"]
    assert rows[-1]["data"]["resume_with"] == f"openshard osn resume {run_id}"
    assert ckpt.read_checkpoint(repo, run_id).status == "interrupted"

    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider",
                        lambda n, m: ("fake", FakeProvider([_turn(_write("out.txt", "ok"), _finish())])))
    r2 = CliRunner().invoke(cli, ["osn", "resume", run_id, "--json-events"])
    assert r2.exit_code == 0, r2.output
    rows2 = _lines(r2.output)
    assert rows2[0]["event"] == "workspace_ready" and rows2[0]["data"]["resumed"] is True
    assert rows2[-1]["event"] == "result" and rows2[-1]["data"]["status"] == "verified"
    assert rows2[-1]["data"]["resumed"]["attempts_restored"] == 1


def test_json_and_text_modes_are_unchanged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    r = _invoke(monkeypatch, repo, FakeProvider([_turn(_write("out.txt", "ok"), _finish())]), "--json")
    body = json.loads(r.output)  # one object, no event lines
    assert body["status"] == "verified"
    text = _invoke(monkeypatch, _repo(tmp_path / "t"), FakeProvider([_turn(_write("out.txt", "ok"), _finish())]))
    assert "OSN loop: verified" in text.output and "{" not in text.output.splitlines()[0]
