"""Every path that stores free text about a session redacts the same forms.

Live capture (task excerpt, shell command), historical import and
verification text all go through ``scrub_sensitive_text``. Historical import
and verification text used to apply only the credential scrubber, so an
imported transcript could store and sync an e-mail address, a JWT or a
private-key block the live path would have removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openshard.adapters.claude_code_import import _sanitize_task
from openshard.adapters.claude_hooks import _scrubbed_command
from openshard.history.verification import _safe_text
from openshard.ingest.normalize import scrub_free_text

EMAIL = "alice.smith@example.com"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"

SCRUBBERS = {
    "live task": lambda text: _sanitize_task(text, placeholder="", cap=2_000),
    "live command": _scrubbed_command,
    "historical import": lambda text: scrub_free_text(text, 2_000, Path.cwd()),
    "verification": lambda text: _safe_text(text, 2_000),
}


@pytest.mark.parametrize("path", sorted(SCRUBBERS))
@pytest.mark.parametrize(
    "sensitive, placeholder",
    [(EMAIL, "[email]"), (JWT, "[jwt]"), (PEM, "[private-key]")],
    ids=["email", "jwt", "private-key"],
)
def test_every_storage_path_redacts_the_same_forms(path: str, sensitive: str, placeholder: str) -> None:
    out = SCRUBBERS[path](f"deploy for {sensitive} then report")

    assert out is not None
    assert placeholder in out
    for fragment in (EMAIL, "eyJhbGciOiJIUzI1NiJ9", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"):
        assert fragment not in out


@pytest.mark.parametrize("path", sorted(SCRUBBERS))
def test_ordinary_text_passes_through(path: str) -> None:
    assert SCRUBBERS[path]("Add a calculator module with unit tests") == (
        "Add a calculator module with unit tests"
    )
