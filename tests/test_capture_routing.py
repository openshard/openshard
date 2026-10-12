"""Unit coverage for the checkout resolver's command-directory hint."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from openshard.adapters.claude_capture_routing import command_directory_hint


@pytest.mark.parametrize("command", [
    "cd /tmp/wt && git commit -m x",
    "cd /tmp/wt; git status",
    "  cd '/tmp/wt' && pytest -q",
    'cd "/tmp/wt" && ls',
])
def test_a_leading_cd_names_the_directory(command):
    assert command_directory_hint(command, "/home/me/project") == str(Path("/tmp/wt"))


def test_a_relative_cd_is_joined_to_the_cwd():
    assert command_directory_hint("cd sub && make", "/home/me/project") == str(Path("/home/me/project") / "sub")
    assert command_directory_hint("cd sub && make", None) is None


@pytest.mark.skipif(os.name != "nt", reason="Git Bash drive paths only matter on Windows")
def test_a_git_bash_drive_path_becomes_a_windows_path():
    assert command_directory_hint("cd /c/tmp/wt && git log", "C:/project") == str(Path("C:/tmp/wt"))


@pytest.mark.parametrize("command", [
    "git -C /tmp/wt status",
    "pytest -q && cd /tmp/wt",
    "cd",
    "cd -",
    "echo cd /tmp/wt && ls",
    None,
    42,
])
def test_anything_else_gives_no_hint(command):
    assert command_directory_hint(command, "/home/me/project") is None
