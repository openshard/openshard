"""The planner and executor see a bounded, observed repository map on their first turn.

Directories with counts, file roles by fixed path rules and the top-level
definitions of source files, derived from the files themselves. It is cut to
fit and says so; nothing in it is model output or an inferred architecture.
The Receipt records only counts (osn_loop.repo_map), never the text.
"""
from __future__ import annotations

from pathlib import Path

from openshard.osn.agent_loop import TurnState
from openshard.osn.model_provider import IterativeModelProvider, build_turn_prompt
from openshard.osn.outline import top_level_symbols
from openshard.osn.repo_map import (
    MAX_MAP_CHARS,
    MAX_MAP_FILES,
    MAX_MAP_SYMBOLS,
    build_repo_map,
    file_role,
)
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY_SRC = (
    "import re\n\n"
    "class Receipt:\n"
    "    def render(self):\n"
    "        return 1\n\n"
    "async def fetch(x):\n"
    "    pass\n\n"
    "def _row(label):\n"
    "    return label\n"
)
JS_SRC = (
    "export default class Store {}\n"
    "export function load(x) { return x }\n"
    "const save = async (x) => x\n"
    "let counter = 0\n"
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "pkg" / "receipt.py").write_text(PY_SRC, encoding="utf-8")
    (root / "pkg" / "store.js").write_text(JS_SRC, encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_receipt.py").write_text("def test_render():\n    pass\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "index.js").write_text("function pad() {}\n", encoding="utf-8")
    return root


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_top_level_symbols_keep_only_depth_zero_definitions():
    assert top_level_symbols(PY_SRC, "pkg/receipt.py") == ["class Receipt", "async def fetch", "def _row"]
    assert top_level_symbols(JS_SRC, "pkg/store.js") == ["class Store", "function load", "const save"]
    assert top_level_symbols("x = 1\n", "pkg/plain.py") == []
    assert top_level_symbols("def a():\n pass\n", "notes.txt") == []
    assert top_level_symbols("\n".join(f"def f{i}(): pass" for i in range(20)), "m.py", max_symbols=3) == [
        "def f0", "def f1", "def f2",
    ]


def test_file_roles_follow_fixed_path_rules():
    assert file_role("pkg/receipt.py") == "source"
    assert file_role("main.py") == "entrypoint"
    assert file_role("tests/test_receipt.py") == "tests"
    assert file_role("pkg/receipt_test.py") == "tests"
    assert file_role("src/app.spec.ts") == "tests"
    assert file_role("docs/guide.md") == "docs"
    assert file_role("README.md") == "docs"
    assert file_role("pyproject.toml") == "config"
    assert file_role(".github/workflows/ci.yml") == "ci"
    assert file_role("data/rows.csv") == "data"
    assert file_role("assets/logo.png") == "other"
    assert file_role("pkg\\receipt.py") == "source"  # Windows separators are not identity


def test_map_shows_directories_roles_and_definitions_and_skips_vendored_trees(tmp_path):
    root = _repo(tmp_path)
    m = build_repo_map(root, _files(root))
    assert m.files_total == 9 and m.truncated is False
    assert "pkg/receipt.py: class Receipt, async def fetch, def _row" in m.text
    assert "pkg/store.js: class Store, function load, const save" in m.text
    assert "main.py (entrypoint): def main" in m.text
    assert "tests/test_receipt.py (tests): def test_render" in m.text
    assert "pkg/__init__.py" not in m.text  # nothing defined: no line
    assert "left-pad" not in m.text and "1 in vendored/generated trees not mapped" in m.text
    assert "pkg/ (3)" in m.text and "docs 1" in m.text and "ci 1" in m.text and "config 1" in m.text
    # Entrypoints first, then source, then tests.
    assert m.text.index("main.py (entrypoint)") < m.text.index("pkg/receipt.py") < m.text.index("tests/test_receipt.py")
    assert m.files_mapped == 4 and m.symbols == 8
    rec = m.to_record()
    assert rec["files_total"] == 9 and rec["files_mapped"] == 4 and rec["symbols"] == 8
    assert rec["chars"] == len(m.text) and rec["source"] == "observed_file_structure"
    assert rec["roles"] == {"ci": 1, "config": 1, "docs": 1, "entrypoint": 1, "source": 3, "tests": 1}
    assert "Receipt" not in str(rec) and "receipt.py" not in str(rec)  # counts only


def test_map_is_bounded_and_says_when_it_was_cut(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    body = "\n".join(f"def function_{i}(): pass" for i in range(MAX_MAP_SYMBOLS + 5)) + "\n"
    for i in range(MAX_MAP_FILES + 10):
        (root / f"module_{i:03d}.py").write_text(body, encoding="utf-8")
    m = build_repo_map(root, _files(root))
    assert m.truncated is True and m.files_mapped <= MAX_MAP_FILES and len(m.text) <= MAX_MAP_CHARS + 200
    assert "(+5 more)" in m.text and "map capped at" in m.text
    assert m.symbols == m.files_mapped * MAX_MAP_SYMBOLS


def test_map_never_raises_on_unreadable_or_missing_files(tmp_path):
    m = build_repo_map(tmp_path, ["gone.py", "", "also/gone.ts"])
    assert m.files_mapped == 0 and "Top-level definitions" in m.text


class _Provider(BaseProvider):
    def __init__(self):
        self.prompts: list[str] = []

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        return ChatResponse('{"actions": [{"kind": "finish"}], "note": "x"}', model, UsageStats(1, 1, 2, 0.0))


def _state(files: list[str], turn: int) -> TurnState:
    return TurnState(task="t", attempt=1, turn=turn, max_turns=5, repo_files=files, observations=[],
                     changed_files=[], blocked_paths=[], previous_failure=None, verifications_left=2,
                     writes_applied=0)


def test_turn_prompt_shows_the_map_on_turn_one_and_a_reminder_after(tmp_path):
    root = _repo(tmp_path)
    m = build_repo_map(root, _files(root))
    first = build_turn_prompt(_state(_files(root), 1), root, [], repo_map=m.text)
    later = build_turn_prompt(_state(_files(root), 2), root, [], repo_map=m.text)
    assert m.text in first and "Repository files:" in first
    assert m.text not in later and "Repository map shown on turn 1" in later
    assert "Repository map" not in build_turn_prompt(_state(_files(root), 1), root, [])


def test_provider_builds_the_map_once_and_records_counts(tmp_path):
    root = _repo(tmp_path)
    fp = _Provider()
    provider = IterativeModelProvider(fp, ["m/a"], root)
    files = _files(root)
    provider.turn(_state(files, 1))
    provider.turn(_state(files, 2))
    assert provider.repo_map is not None and provider.repo_map.files_mapped == 4
    assert "pkg/receipt.py: class Receipt" in fp.prompts[0] and "pkg/receipt.py: class Receipt" not in fp.prompts[1]
    assert provider.repo_map_record() == provider.repo_map.to_record()
