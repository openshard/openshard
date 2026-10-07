"""Verification output is read as UTF-8 on every platform.

On Windows the locale codec is cp1252; a single byte it cannot decode in a
test's output (a box-drawing glyph, an arrow in a Receipt rendering test)
raised inside subprocess's reader thread, the output was lost and the
verification read as the change's failure. The outcome of a check must not
depend on the characters a test prints.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from openshard.osn.loop import _run_verification, run_bounded_loop
from openshard.osn.model_provider import ModelActionProvider
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
# 0x81 is undefined in cp1252 and not valid UTF-8 on its own: the worst case for both codecs.
ODD_SCRIPT = b"import sys\nsys.stdout.buffer.write(b'ok \\x81 \\xe2\\x86\\xb3 done\\n')\nsys.exit(0)\n"


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001))


def _odd_command(where: Path) -> list[str]:
    script = where / "odd_output.py"
    script.write_bytes(ODD_SCRIPT)
    return [PY, str(script)]


def test_undecodable_bytes_in_check_output_do_not_lose_the_output_or_the_verdict(tmp_path):
    result, output = _run_verification(_odd_command(tmp_path), tmp_path, 60.0)
    assert result.ran and result.passed and result.exit_code == 0, output
    assert output.startswith("ok ") and output.rstrip().endswith("↳ done")
    assert "�" in output  # the undecodable byte is replaced, never raised


def test_a_run_whose_check_prints_odd_bytes_still_verifies(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out.txt").write_text("bad")
    command = _odd_command(tmp_path)
    provider = ModelActionProvider(FakeProvider([json.dumps({"writes": [{"path": "out.txt", "content": "ok"}]})]),
                                   ["m"], repo)
    receipt = run_bounded_loop(repo, "t", provider, command, max_attempts=1)
    assert receipt.status == "verified" and receipt.attempts[0].verification.passed
