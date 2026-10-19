"""``openshard.util.paths``: one key per location, however the path is spelled.

Windows spellings are checked on any OS by swapping in ``ntpath`` (the rules
``os.path`` follows on Windows); resolving short names needs a real Windows
filesystem and is covered by the Windows CI job.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

import pytest

from openshard.adapters import capture_auth, commit_usage_trailers
from openshard.util import paths


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(paths, "_pathmod", ntpath)


@pytest.mark.parametrize("spelling", [
    r"C:\Users\Dev\Repo",
    "C:/Users/Dev/Repo",
    r"c:\users\dev\repo",
    "C:/Users/Dev/Repo/",
    r"C:\Users\Dev/Repo\.",
    r"C:\Users\Dev\other\..\Repo",
])
def test_windows_spellings_of_one_directory_share_a_key(windows, spelling):
    assert paths.path_key(spelling) == paths.path_key(r"C:\Users\Dev\Repo")


def test_windows_keeps_different_directories_apart(windows):
    assert paths.path_key(r"C:\Users\Dev\Repo") != paths.path_key(r"C:\Users\Dev\Repo2")
    assert paths.path_key(r"C:\Repo") != paths.path_key(r"D:\Repo")


def test_windows_absolute_paths(windows):
    assert paths.is_absolute(r"C:\x") and paths.is_absolute("C:/x")
    assert not paths.is_absolute("x") and not paths.is_absolute(".")


def test_posix_resolves_symlinks_and_keeps_case(tmp_path):
    real = tmp_path / "Real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert paths.path_key(link) == paths.path_key(real) == paths.path_key(f"{real}{os.sep}")
    if os.name != "nt":
        assert paths.path_key(tmp_path / "real") != paths.path_key(real)


def test_accepts_str_bytes_and_path_like(tmp_path):
    assert paths.path_key(tmp_path) == paths.path_key(str(tmp_path)) == paths.path_key(os.fsencode(tmp_path))


def test_capability_root_spelling_is_unchanged(tmp_path):
    """The HMAC input must not change: existing capabilities keep verifying."""
    expected = os.path.normcase(os.path.normpath(os.path.realpath(str(tmp_path))))
    assert capture_auth.normalise_root(tmp_path) == expected == paths.path_key(tmp_path)


def test_a_command_names_the_repository_however_git_spells_it(windows):
    """Git prints ``C:/...``; the agent's shell wrote ``C:\\...`` in another case."""
    command = r"cd C:\Users\Dev\Repo && git commit -F m"
    assert commit_usage_trailers._names(command, "C:/Users/dev/repo")
    assert commit_usage_trailers._names('cd "C:\\Users\\Dev\\Repo" && git commit', "C:/Users/Dev/Repo")
    assert not commit_usage_trailers._names(r"cd D:\elsewhere && git commit -F m", "C:/Users/Dev/Repo")


def test_a_relative_word_never_names_the_repository(tmp_path, monkeypatch):
    """``git add .`` in another agent's checkout says nothing about this process's directory."""
    monkeypatch.chdir(tmp_path)
    assert not commit_usage_trailers._names("cd . && git commit -F m", str(Path.cwd()))
