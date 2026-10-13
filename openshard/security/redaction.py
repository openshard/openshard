"""Local redaction of obviously sensitive forms in free text OpenShard stores.

``security/secret_scan.py`` recognises credentials (API keys, tokens,
``password=`` assignments) and is also the *file* scanner whose findings
become unsafe proof findings. The forms below are different: an e-mail
address, a JWT or a PEM private-key block in a prompt excerpt or a shell
command is something OpenShard should not keep, but their presence in a
repository file is not by itself a finding (LICENSE files carry e-mails).
So this module is applied only to text OpenShard stores about a session
-- the task excerpt and the command text -- never to the secret scanner.
A credential written in prose ("the password is ...") is redacted here
too: it is what a person types into a prompt, and the assignment rule of
the secret scanner does not see it.

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
PLACEHOLDER_CREDENTIAL = "[credential]"

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

# A credential written in prose, as people type it into a prompt: "the
# password is hunter2", "my passphrase was: x", "API key: abc123",
# "secret = x". Only the value after the keyword is replaced, so the sentence
# stays readable. The secret scanner's assignment rule needs 12+ characters
# and ``:``/``=``; ordinary passwords are shorter and introduced with "is".
# Only password words take "is"/"was": "the token is cached" or "tokens are
# counted" is ordinary text in a product about LLM tokens. Bare "pass" is
# never a keyword (tests pass: 5).
_PASSWORD_PROSE_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|pwd)\b"
    r"(\s+(?:is|was)\b\s*:?\s*|\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|auth[ _-]?token|token|client[ _-]?secret|secret)\b"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private_key", _PEM_BLOCK_RE, PLACEHOLDER_PRIVATE_KEY),
    ("jwt", _JWT_RE, PLACEHOLDER_JWT),
    ("email", _EMAIL_RE, PLACEHOLDER_EMAIL),
    ("credential", _PASSWORD_PROSE_RE, rf"\1\2{PLACEHOLDER_CREDENTIAL}"),
    ("credential", _CREDENTIAL_ASSIGNMENT_RE, rf"\1\2{PLACEHOLDER_CREDENTIAL}"),
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


def scrub_sensitive_text(text: str, *, source_label: str) -> str:
    """The scrub every stored free-text string goes through.

    Secret-like values first (``scrub_text_for_secrets``), then the e-mail,
    JWT and private-key forms above. Use this instead of calling either step
    alone, so live capture, historical import and verification text cannot
    drift apart. *source_label* names the caller in secret-scan findings.
    """
    from openshard.security.secret_scan import scrub_text_for_secrets

    scrubbed, _ = scrub_text_for_secrets(text, source_label=source_label)
    redacted, _kinds = redact_sensitive_text(scrubbed)
    return redacted


__all__ = [
    "PLACEHOLDER_CREDENTIAL",
    "PLACEHOLDER_EMAIL",
    "PLACEHOLDER_JWT",
    "PLACEHOLDER_PRIVATE_KEY",
    "redact_sensitive_text",
    "scrub_sensitive_text",
]
