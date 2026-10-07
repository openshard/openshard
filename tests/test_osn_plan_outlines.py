"""A plan file too large to show whole is shown as a line-numbered outline on the executor's first turn.

With the file's definitions and their line numbers in hand, the executor
reads the range it needs instead of paging through thousands of lines.
Small plan files are still inlined; files in languages the outline does
not understand are left to read_file. The Receipt records which files were
outlined under osn_loop.plan_outline_files.
"""
from __future__ import annotations

from pathlib import Path

from openshard.osn.agent_loop import TurnState
from openshard.osn.model_provider import (
    MAX_CONTEXT_FILE_BYTES,
    IterativeModelProvider,
    build_turn_prompt,
    plan_context_files,
    plan_outline_files,
)
from openshard.osn.outline import outline_entries, render_outline
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY_SRC = (
    '"""Module."""\n'
    "import re\n"
    "\n"
    "class Receipt:\n"
    "    def render(self):\n"
    "        return 1\n"
    "\n"
    "    def _inner(self):\n"
    "        def deeper():\n"
    "            pass\n"
    "        return deeper\n"
    "\n"
    "\n"
    "async def fetch(x):\n"
    "    pass\n"
    "\n"
    "def _row(label, value, width=12):\n"
    "    return label\n"
)


class FakeProvider(BaseProvider):
    def __init__(self):
        self.prompts: list[str] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        return ChatResponse("{}", model, UsageStats(1, 1, 2, 0.0))


def test_python_outline_lists_definitions_with_line_numbers_one_level_deep():
    entries = outline_entries(PY_SRC, "x.py")
    assert entries == [
        (4, "class Receipt:"),
        (5, "  def render(self):"),
        (8, "  def _inner(self):"),
        (14, "async def fetch(x):"),
        (17, "def _row(label, value, width=12):"),
    ]
    text = render_outline(PY_SRC, "x.py")
    assert text.startswith("18 lines; definitions start at:\n 4: class Receipt:\n 5:   def render(self):")
    assert "deeper" not in text


def test_javascript_and_unknown_languages():
    js = "export default function main() {}\nconst helper = (a) => a;\nclass Box {\n  render() {}\n}\nlet x = 1;\n"
    assert outline_entries(js, "app.tsx") == [(1, "export default function main() {}"), (2, "const helper = (a) => a;"),
                                              (3, "class Box {")]
    assert outline_entries("def x():\n  pass\n", "notes.md") == [] and render_outline("x", "a.go") == ""


def test_the_outline_is_capped():
    big = "\n".join(f"def f{i}():\n    pass" for i in range(300))
    entries = outline_entries(big, "big.py", max_entries=10)
    assert len(entries) == 10 and entries[-1][0] == 19
    assert render_outline(big, "big.py", max_entries=10).endswith("... (outline capped at 10 definitions)")


def _state(turn: int) -> TurnState:
    return TurnState(task="t", attempt=1, turn=turn, max_turns=12, repo_files=["big.py"], observations=[],
                     changed_files=[], blocked_paths=[], previous_failure=None, verifications_left=2,
                     writes_applied=0)


def test_large_plan_files_are_outlined_on_turn_one_and_small_ones_inlined(tmp_path: Path):
    (tmp_path / "small.py").write_text("def small():\n    return 1\n")
    big = "\n".join(f"def f{i}():\n    return {i}" for i in range(2000)) + "\n"
    assert len(big.encode()) > MAX_CONTEXT_FILE_BYTES
    (tmp_path / "big.py").write_text(big)
    (tmp_path / "data.bin").write_bytes(b"\x00" * (MAX_CONTEXT_FILE_BYTES + 10))
    plan = {"files": ["small.py", "big.py", "data.bin", "missing.py"]}
    assert plan_context_files(plan, tmp_path, []) == ["small.py"]
    assert plan_outline_files(plan, tmp_path, []) == ["big.py"]  # data.bin: no outline for that language

    provider = IterativeModelProvider(FakeProvider(), ["m"], tmp_path)
    provider.set_plan(plan)
    assert provider.plan_context_files == ["small.py"] and provider.plan_outline_files == ["big.py"]
    first = build_turn_prompt(_state(1), tmp_path, provider.plan_context_files, plan=plan,
                              outline_files=provider.plan_outline_files)
    assert '<untrusted file="small.py">\ndef small():' in first
    assert '<untrusted file="big.py" outline="line numbers">\n4000 lines; definitions start at:\n' in first
    assert "\n  1: def f0():\n" in first and "read_file with start_line and max_lines" in first
    assert "def f1999" not in first  # capped: the outline never grows with the file
    second = build_turn_prompt(_state(2), tmp_path, provider.plan_context_files, plan=plan,
                               outline_files=provider.plan_outline_files)
    assert "outline=" not in second and "Outlines shown to you on turn 1: big.py" in second
