"""Concise, human-readable task titles for Receipts.

A task title is *display metadata*: a short label such as
``"Fix CORS in the private Openshard Platform repo"`` for a Receipt whose task was
``"We need a very small CORS fix in the private Openshard Platform repo..."``.
It never replaces the task text (``task_full`` stays the recorded task) and is
never evidence of anything.

Flow
----
1. Capture writes the record immediately with a deterministic title
   (:func:`derive_task_title`) -- no model call ever sits on the capture path.
2. An optional :class:`TitleGenerator` (e.g. a small, fast model) may improve
   the title later via :func:`improve_task_title_async`, off the capture
   thread; its output is re-validated and silently dropped if unsafe.
3. Readers resolve the title with :func:`resolve_task_title`: a stored title
   when it is valid, otherwise a fresh derivation from the task text -- so
   records written before titles existed still display a clean title.

No generator is wired by default: title generation is deterministic and local
unless a caller supplies one.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

TITLE_MAX_CHARS = 60
TITLE_MAX_WORDS = 9
UNTITLED = "Untitled task"

# How much task text is ever looked at. Titles come from the opening of a task.
_SCAN_CHARS = 2_000

# Transport wrappers whose *content* is not the task (slash-command echoes etc.).
_RE_NOISE_BLOCK = re.compile(
    r"<(command-[\w-]+|local-command-[\w-]+|system-reminder)\b[^>]*>.*?</\1\s*>", re.S | re.I
)
_RE_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
_RE_CODE_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_RE_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_URL_MARK = "\x00"
# A URL and the words that only point at it ("see <url> and <url>") become a
# sentence break, so the surrounding instruction reads cleanly.
_RE_URL_PHRASE = re.compile(
    r"(?:\b(?:see|check(?: out)?|visit|open|read|per|at|from|via|in|on)\s+)?\x00"
    r"(?:\s*(?:,|and)?\s*\x00)*(?:\s*(?:,|and)\b)?",
    re.I,
)
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+|\s+[-–—]\s+")
_RE_WS = re.compile(r"\s+")

# Conversational framing removed from the start of a sentence (repeatedly).
_FRAMING = [
    r"ok(?:ay)?", r"alright", r"hi", r"hey", r"hello", r"so", r"now", r"then",
    r"also", r"next", r"right", r"great", r"thanks", r"thank you", r"cool",
    r"please", r"pls", r"kindly", r"just", r"quickly", r"basically", r"actually",
    r"(?:can|could|would|will) you(?: please)?",
    r"(?:i|we) (?:want|need|would like|'d like|d like) (?:you )?to",
    r"(?:i|we) (?:want|need|would like|'d like|d like)",
    r"(?:i|we)'?d like (?:you )?to",
    r"(?:i|we) (?:think|guess|believe) (?:we|you) should",
    r"(?:we|you) should",
    r"(?:we|you) (?:have|need) to",
    r"(?:i|we) am going to|(?:i|we) are going to|(?:i|we)'re going to|(?:i|we)'m going to",
    r"let'?s(?: now)?", r"help me(?: to)?", r"go ahead and", r"make sure to",
    r"your (?:task|job) is to", r"the (?:task|goal) is to", r"task:", r"goal:",
]
_RE_FRAMING = re.compile(r"^(?:" + "|".join(_FRAMING) + r")\b[\s,:]*", re.I)

# Sentences that carry no task content on their own.
_RE_FILLER_SENTENCE = re.compile(
    r"^(?:we are|we're|i am|i'm) (?:moving|going|working) (?:quickly|fast|slowly)\b.*"
    r"|^(?:thanks|thank you|hi|hello|hey|ok(?:ay)?|great|cool|perfect|nice)\b.*"
    r"|^(?:this|it) (?:is|should be) (?:a )?(?:small|quick|simple|tiny)\b.*",
    re.I,
)

# Task nouns that become the title's leading verb ("a CORS fix" -> "Fix CORS").
_NOUN_TO_VERB = {
    "fix": "Fix", "fixes": "Fix", "bugfix": "Fix", "improvement": "Improve",
    "improvements": "Improve", "update": "Update", "updates": "Update",
    "change": "Change", "changes": "Change", "tweak": "Tweak", "tweaks": "Tweak",
    "refactor": "Refactor", "cleanup": "Clean up", "clean-up": "Clean up",
    "addition": "Add", "edit": "Edit", "edits": "Edit", "review": "Review",
    "upgrade": "Upgrade", "migration": "Migrate", "rewrite": "Rewrite",
    "investigation": "Investigate", "optimization": "Optimize",
    "optimisation": "Optimise", "adjustment": "Adjust", "correction": "Correct",
}
_QUANTIFIERS = r"(?:a|an|one|the|some|another|this|that|two|three|few)"
_SOFTENERS = (
    r"(?:very|really|super|quite|pretty|tiny|small|little|quick|minor|simple|"
    r"single|focused|focussed|narrow|targeted|surgical|short|slight|light|"
    r"final|last|new|further|more)"
)
_RE_NOUN_TASK = re.compile(
    r"^(?:(?:make|do|ship|land|implement|apply|add|create|have|get|need|want|"
    r"there is|there's)\s+)?"
    rf"(?:{_QUANTIFIERS}\s+)?(?:{_SOFTENERS}[\s,]+)*"
    r"(?P<pre>(?:[\w./+#-]+\s+){0,3}?)"
    r"(?P<noun>" + "|".join(sorted(map(re.escape, _NOUN_TO_VERB), key=len, reverse=True)) + r")"
    r"(?:\s+(?P<rest>(?:to|for|in|on|of|at|with|around|across)\b.*))?$",
    re.I,
)
_RE_LEAD_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)
_RE_PREP_ARTICLE = re.compile(r"\b(to|for|in|on|of|at|with|around|across) the\s+", re.I)
_RE_TRAILING_CONNECTIVE = re.compile(
    r"(?:\s+|^)(?:and|or|with|for|but|as|of|to|in|a|an|the|so|that|which|when|"
    r"where|while|by|on|at|from|into|its|their|this|these|our|my|your|is|are|"
    r"be|then|also|just|very|because|if)$",
    re.I,
)
_RE_CLAUSE_BREAK = re.compile(
    r",\s+|\s+(?:so that|because|since|but|which|while|without|and then|then|when|"
    r"whenever|if|unless|until|after|before|as soon as|so)\s+",
    re.I,
)
_RE_EDGE_PUNCT = re.compile(r"^[\s\"'`*_#>~()\[\]{}:,;.!?-]+|[\s\"'`*_#>~()\[\]{}:,;.!?-]+$")
_REDACTED = re.compile(r"\w\.\.\.\w|\*\*\*")


def _looks_like_secret(word: str) -> bool:
    from openshard.safety.sanitize import looks_like_secret

    if looks_like_secret(word) or _REDACTED.search(word):
        return True
    if "=" in word:
        return True
    # Long opaque tokens (keys, hashes, ids) have no place in a title.
    core = word.strip(".,;:!?\"'()[]{}")
    return len(core) >= 20 and bool(re.search(r"\d", core)) and bool(re.search(r"[A-Za-z]", core))


def _clean_text(text: str) -> str:
    """Strip markup, URLs, emails and secret-like tokens; collapse whitespace."""
    from openshard.security.secret_scan import scrub_text_for_secrets

    text = text[:_SCAN_CHARS]
    text, _ = scrub_text_for_secrets(text, source_label="<task-title>")
    text = _RE_CODE_FENCE.sub(" ", text)
    text = _RE_NOISE_BLOCK.sub(" ", text)
    text = _RE_TAG.sub(" ", text)
    text = _RE_URL_PHRASE.sub("\n", _RE_URL.sub(_URL_MARK, text))
    text = _RE_EMAIL.sub(" ", text)
    text = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in text)
    lines = []
    for line in text.split("\n"):
        words = [w for w in line.split() if not _looks_like_secret(w)]
        lines.append(" ".join(words))
    return "\n".join(lines)


def _strip_framing(sentence: str) -> str:
    prev = None
    s = sentence.strip()
    while s and s != prev:
        prev = s
        s = _RE_FRAMING.sub("", s).strip()
    return s


def _nominal_to_imperative(s: str) -> str:
    """``"a very small CORS fix in the repo"`` -> ``"Fix CORS in the repo"``."""
    m = _RE_NOUN_TASK.match(s)
    if not m:
        return s
    verb = _NOUN_TO_VERB[m.group("noun").lower()]
    pre = (m.group("pre") or "").strip()
    rest = (m.group("rest") or "").strip()
    if rest:
        # "an improvement to X" -> "Improve X"; "a fix for X" -> "Fix X"
        prep, _, obj = rest.partition(" ")
        if prep.lower() in {"to", "for", "of", "on"} and obj:
            rest = obj
    parts = [verb, pre, rest]
    return " ".join(p for p in parts if p)


def _clamp(title: str) -> str:
    """Cut at a clause break, then to the word/char budget, at a word boundary."""
    if len(title) > TITLE_MAX_CHARS or len(title.split()) > TITLE_MAX_WORDS:
        head = _RE_CLAUSE_BREAK.split(title, maxsplit=1)[0]
        if len(head.split()) >= 3:
            title = head
    words = title.split()[:TITLE_MAX_WORDS]
    while words and len(" ".join(words)) > TITLE_MAX_CHARS:
        words.pop()
    if not words:  # a single enormous word
        return title[:TITLE_MAX_CHARS].rstrip()
    title = " ".join(words)
    while True:
        trimmed = _RE_TRAILING_CONNECTIVE.sub("", title).strip()
        trimmed = _RE_EDGE_PUNCT.sub("", trimmed)
        if trimmed == title or not trimmed:
            break
        title = trimmed
    return title


def _finish(s: str) -> str:
    s = _RE_EDGE_PUNCT.sub("", _RE_WS.sub(" ", s)).strip()
    if not s:
        return ""
    s = _clamp(s)
    s = _RE_EDGE_PUNCT.sub("", s).strip()
    return s[:1].upper() + s[1:] if s else ""


def _title_from_sentence(sentence: str) -> str:
    s = _strip_framing(sentence)
    if not s or _RE_FILLER_SENTENCE.match(s):
        return ""
    s = _nominal_to_imperative(s)
    s = _RE_LEAD_ARTICLE.sub("", s) if len(s.split()) > 2 else s
    # "Improve the X" -> "Improve X": articles after the verb add nothing.
    first, _, tail = s.partition(" ")
    tail = _RE_LEAD_ARTICLE.sub("", tail)
    s = f"{first} {tail}".strip()
    if len(s) > TITLE_MAX_CHARS:
        s = _RE_PREP_ARTICLE.sub(r"\1 ", s)
    return _finish(s)


def derive_task_title(task: object) -> str:
    """Deterministic, local title for *task*. Never raises; never calls a model.

    Returns :data:`UNTITLED` for empty/unusable input. The result is at most
    :data:`TITLE_MAX_CHARS` characters, contains no URLs or secret-like
    tokens, and has conversational framing ("We need", "I want", "please")
    removed.
    """
    try:
        if not isinstance(task, str) or not task.strip():
            return UNTITLED
        text = _clean_text(task)
        sentences = [s for s in _RE_SENTENCE_SPLIT.split(text) if s and s.strip()]
        fallback = ""
        for sentence in sentences[:8]:
            title = _title_from_sentence(sentence)
            if not title:
                continue
            if len(title.split()) >= 2:
                return title
            fallback = fallback or title
        return fallback or UNTITLED
    except Exception:
        return UNTITLED


def normalize_title_candidate(candidate: object) -> str | None:
    """Validate and tidy a proposed title (stored or model-generated).

    Returns the cleaned title, or ``None`` when *candidate* is unusable: empty,
    multi-line, URL- or secret-bearing, or reduced to nothing by cleaning.
    Never raises.
    """
    try:
        if not isinstance(candidate, str):
            return None
        raw = candidate.strip()
        if not raw or "\n" in raw or len(raw) > TITLE_MAX_CHARS * 3:
            return None
        if _RE_URL.search(raw) or _RE_EMAIL.search(raw):
            return None
        if any(_looks_like_secret(w) for w in raw.split()):
            return None
        title = _title_from_sentence(raw)
        return title or None
    except Exception:
        return None


def resolve_task_title(entry: Mapping[str, object]) -> str:
    """The display title for a stored record: its valid ``task_title``, or a
    derivation from its ``task`` (records written before titles existed)."""
    stored = normalize_title_candidate(entry.get("task_title"))
    if stored:
        return stored
    return derive_task_title(entry.get("task"))


@runtime_checkable
class TitleGenerator(Protocol):
    """Optional title improver (e.g. a small, fast model behind an existing
    provider). Implementations receive already secret-scrubbed task text and
    return a candidate title, or ``None``. They may be slow; they are never
    called on the capture path."""

    def generate_title(self, task: str) -> str | None: ...


def improve_task_title(task: str, fallback: str, generator: TitleGenerator | None) -> str:
    """Ask *generator* for a better title, keeping *fallback* unless the
    candidate passes :func:`normalize_title_candidate`. Never raises."""
    if generator is None or not isinstance(task, str) or not task.strip():
        return fallback
    try:
        candidate = generator.generate_title(_clean_text(task))
    except Exception:
        return fallback
    return normalize_title_candidate(candidate) or fallback


def improve_task_title_async(
    task: str,
    fallback: str,
    generator: TitleGenerator | None,
    persist: Callable[[str], None],
) -> threading.Thread | None:
    """Improve a title on a daemon thread and hand the result to *persist*.

    Returns immediately (``None`` when there is no generator). *persist* is
    only called when the improved title differs from *fallback*; it should
    store the title as display metadata beside the record, never by rewriting
    the hashed record itself.
    """
    if generator is None:
        return None

    def _run() -> None:
        title = improve_task_title(task, fallback, generator)
        if title != fallback:
            try:
                persist(title)
            except Exception:
                pass

    thread = threading.Thread(target=_run, name="openshard-task-title", daemon=True)
    thread.start()
    return thread


__all__ = [
    "TITLE_MAX_CHARS",
    "TITLE_MAX_WORDS",
    "UNTITLED",
    "TitleGenerator",
    "derive_task_title",
    "improve_task_title",
    "improve_task_title_async",
    "normalize_title_candidate",
    "resolve_task_title",
]
