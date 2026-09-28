"""Local redaction of obviously sensitive forms in free text OpenShard stores.

``security/secret_scan.py`` recognises credentials (API keys, tokens,
``password=`` assignments) and is also the *file* scanner whose findings
become unsafe proof findings. The forms below are different: an e-mail
address, a JWT or a PEM private-key block in a prompt excerpt or a shell
command is something OpenShard should not keep, but their presence in a
repository file is not by itself a finding (LICENSE files carry e-mails).
So this module is applied only to text OpenShard stores about a session
-- the task excerpt and the command text -- never to the secret scanner.

Conservative on purpose: each pattern needs the full documented shape
(three base64url segments starting ``eyJ`` for a JWT, ``-----BEGIN ...
PRIVATE KEY-----`` for PEM, ``local@domain.tld`` for an address). A match
is replaced by a fixed placeholder; nothing about the match is kept.
Pure, never raises.
"""

from __future__ import annotations

import re

PLACEHOLDER_EMAIL = "[email]"
PLACEHOLDER_JWT = "[jwt]"
PLACEHOLDER_PRIVATE_KEY = "[private-key]"

# Order matters: a PEM block is removed before anything inside it is examined.
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
# Three base64url segments; the header of every JSON JWT encodes to ``eyJ``.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# local@domain.tld with a real TLD; no quoted locals or IP literals (rare, and
# a false negative there costs less than mangling ordinary text).
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![\w-])")

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private_key", _PEM_BLOCK_RE, PLACEHOLDER_PRIVATE_KEY),
    ("jwt", _JWT_RE, PLACEHOLDER_JWT),
    ("email", _EMAIL_RE, PLACEHOLDER_EMAIL),
)


def redact_sensitive_text(text: str) -> tuple[str, list[str]]:
    """Return ``(redacted_text, kinds_found)``; *kinds_found* is a de-duplicated list."""
    if not isinstance(text, str) or not text:
        return text, []
    kinds: list[str] = []
    out = text
    try:
        for kind, pattern, placeholder in _PATTERNS:
            out, n = pattern.subn(placeholder, out)
            if n and kind not in kinds:
                kinds.append(kind)
    except Exception:
        return text, kinds
    return out, kinds


__all__ = [
    "PLACEHOLDER_EMAIL",
    "PLACEHOLDER_JWT",
    "PLACEHOLDER_PRIVATE_KEY",
    "redact_sensitive_text",
]
