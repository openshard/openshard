"""What a human's follow-up message to a coding agent was for (``rules-v1``).

Deterministic, local and explainable: a short ordered list of English
phrase rules, no model and no network. It runs only on the user's machine,
inside the capture path, on prompt text that is discarded straight after
(``adapters/steering.py``); only the result -- an intent from a fixed
vocabulary, a confidence, the id of the rule that decided and the prompt's
length in characters -- is ever kept. The rule id is a fixed token from this
module, never text taken from the prompt.

Intents: ``correction`` (the agent got something wrong), ``redirect`` (change
of approach or direction), ``clarification`` (a question), ``approval``
(the work is fine), ``continue`` (carry on), ``new_task`` (more or other
work), ``unknown`` (no rule is sure). Confidence is ``high`` when the message
is, or starts with, an unambiguous phrase; ``medium`` when a marker appears
in the body; ``low`` for a length heuristic. Unknown stays unknown: a
message no rule recognises is ``unknown``, never a best guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

METHOD = "rules-v1"

INTENT_CORRECTION = "correction"
INTENT_REDIRECT = "redirect"
INTENT_CLARIFICATION = "clarification"
INTENT_APPROVAL = "approval"
INTENT_CONTINUE = "continue"
INTENT_NEW_TASK = "new_task"
INTENT_UNKNOWN = "unknown"
INTENTS: frozenset[str] = frozenset({
    INTENT_CORRECTION, INTENT_REDIRECT, INTENT_CLARIFICATION, INTENT_APPROVAL,
    INTENT_CONTINUE, INTENT_NEW_TASK, INTENT_UNKNOWN,
})
CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})

# Messages at least this long with no other marker and no reference back to
# the previous work read as a new task (low confidence).
_NEW_TASK_MIN_CHARS = 200


@dataclass(frozen=True)
class Intent:
    intent: str
    confidence: str
    rule: str


def _rx(*phrases: str) -> re.Pattern[str]:
    return re.compile("|".join(phrases))


# Whole-message replies (punctuation, emoji and "please"/"thanks" tails ignored).
_APPROVAL_PHRASES = (
    r"(?:yes|yep|yeah|yup|y|ok|okay|k|sure|lgtm|looks good(?: to me)?|looks great|perfect|great|nice|cool|"
    r"awesome|excellent|good|good job|well done|thanks|thank you|ty|ship it|approved|that works|works now|"
    r"it works|all good|love it|exactly|correct|that's right|that is right)"
)
_APPROVAL_ONLY = re.compile(f"^{_APPROVAL_PHRASES}$")
_APPROVAL_LEAD = re.compile(rf"^{_APPROVAL_PHRASES}\b[\s,.!;:-]*(?P<rest>.+)$")
_CONTINUE_ONLY = _rx(
    r"^(?:continue|go ahead|go on|keep going|carry on|proceed|next|do it|go for it|yes,? (?:continue|go ahead|"
    r"proceed|do it|please)|ok,? (?:continue|go ahead|proceed|do it)|please (?:continue|proceed|go ahead)|"
    r"resume|finish it|finish up|keep at it|you can continue)$"
)
_CONTINUE_START = _rx(r"^(?:continue|go ahead|keep going|carry on|proceed|resume)\b")

# Corrections: the agent did something wrong or not what was asked.
_CORRECTION_START = _rx(
    r"^(?:no|nope|nah|wrong|incorrect|not quite|not really|that's not it|that is not it)\b",
    r"^(?:don't|do not|dont|stop doing|never(?! ?mind))\b",
    r"^(?:redo|undo|revert|roll ?back|try again|fix (?:it|this|that))\b",
)
_CORRECTION_ANY = _rx(
    r"\bthat'?s wrong\b", r"\bthat is wrong\b", r"\bnot what i (?:asked|wanted|meant|said)\b",
    r"\bi (?:don't|do not|didn't|did not) (?:like|want|ask)\b", r"\bmake it (?:exactly )?(?:like|match)\b",
    r"\bexactly like\b", r"\b(?:doesn't|does not|didn't|did not|won't|isn't|is not) work(?:ing)?\b",
    r"\bstill (?:fail|failing|broken|wrong|not|doesn't|does not|the same)\b", r"\b(?:is|are|it's) broken\b",
    r"\byou (?:forgot|missed|broke|ignored|deleted|removed|changed)\b", r"\bshould(?:n't| not| be| have)\b",
    r"\bwrong\b", r"\bredo\b", r"\brevert\b", r"\bundo\b", r"\btry again\b", r"\bbroke\b",
    r"\bnot (?:right|correct)\b", r"\bpreview\b.*\breal\b",
)
# Redirects: a change of approach or direction rather than a fix.
_REDIRECT_START = _rx(
    r"^(?:actually|instead|wait|hold on|hang on|scratch that|forget (?:that|it|about)|never ?mind|"
    r"change of plan|on second thought|let'?s (?:not|instead|switch|go with|use)|rather|switch to|stop)\b",
)
_REDIRECT_ANY = _rx(
    r"\binstead\b", r"\brather than\b", r"\bswitch (?:to|over)\b", r"\bdifferent approach\b",
    r"\bchange (?:the )?(?:approach|plan|direction)\b", r"\bgo with\b", r"\bforget (?:that|it|about)\b",
    r"\bscratch that\b",
)
# New / additional work.
_NEW_TASK_START = _rx(
    r"^(?:also|now (?:do|add|make|write|implement|create|let'?s)|next,|next (?:do|add|let'?s)|and now|"
    r"another thing|one more thing|additionally|new task|separately|can you also|could you also|please also)\b",
)
_QUESTION_START = _rx(
    r"^(?:why|what|how|where|which|when|who|whose|is (?:it|this|that|there)|are (?:there|these|those|you)|"
    r"does|do (?:you|we|i|they)|did (?:you|it|we)|was|were|should (?:i|we)|would (?:it|that)|"
    r"can you explain|could you explain|explain)\b",
)
# "Can you ..." / "could you ..." questions are requests, not clarifications.
_REQUEST_QUESTION = _rx(r"^(?:can|could|would|will) you\b", r"^please\b")
# A reference back to the work so far: a long message with one is not a new task.
_BACK_REFERENCE = _rx(
    r"^(?:this|that|it|these|those|thanks|thank you)\b",
    r"\b(?:above|previous|earlier|again|you just|you did|the change|your change|same)\b",
)

_PASTED_RE = re.compile(r"<pasted_content\b[^<>]*>.*?(?:</pasted_content\s*>|$)", re.IGNORECASE | re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_TAIL_RE = re.compile(r"(?:[\s!.,;:~)(*]|please|thanks|thank you|pls|plz|:\))+$")
_SPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    text = _PASTED_RE.sub(" ", text)
    text = _CODE_FENCE_RE.sub(" ", text)
    text = text.replace("’", "'").replace("‘", "'").lower()
    return _SPACE_RE.sub(" ", text).strip()


def _bare(text: str) -> str:
    """*text* without trailing punctuation, emoji or politeness, for whole-message matches."""
    previous = None
    while previous != text:
        previous = text
        text = _TAIL_RE.sub("", text).strip()
        text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]+$", "", text).strip()
    return text


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0]


def classify_intent(prompt: object) -> Intent:
    """The intent of a follow-up message, deterministically. Never raises; never keeps *prompt*."""
    try:
        return _classify(prompt)
    except Exception:
        return Intent(INTENT_UNKNOWN, "low", "error")


def _classify(prompt: object) -> Intent:
    if not isinstance(prompt, str):
        return Intent(INTENT_UNKNOWN, "low", "empty")
    text = _normalise(prompt)
    if not text:
        return Intent(INTENT_UNKNOWN, "low", "empty")
    bare = _bare(text)
    first = _first_sentence(text)

    if _APPROVAL_ONLY.match(bare):
        return Intent(INTENT_APPROVAL, "high", "approval.whole_message")
    if _CONTINUE_ONLY.match(bare):
        return Intent(INTENT_CONTINUE, "high", "continue.whole_message")
    if _CORRECTION_START.match(first):
        # "no, use X instead" changes direction; a bare "no" / "don't" corrects.
        if _REDIRECT_ANY.search(text) and not _CORRECTION_ANY.search(text):
            return Intent(INTENT_REDIRECT, "medium", "redirect.negation_with_alternative")
        return Intent(INTENT_CORRECTION, "high", "correction.leading_negation")
    if _CORRECTION_ANY.search(first):
        return Intent(INTENT_CORRECTION, "high", "correction.phrase")
    if _REDIRECT_START.match(first):
        return Intent(INTENT_REDIRECT, "high", "redirect.leading_phrase")
    if _NEW_TASK_START.match(first):
        return Intent(INTENT_NEW_TASK, "high", "new_task.leading_phrase")
    if _CONTINUE_START.match(first):
        return Intent(INTENT_CONTINUE, "medium", "continue.leading_phrase")
    # Approval opening a longer message: "looks good, but ..." corrects; "looks good, now ..." adds work.
    lead = _APPROVAL_LEAD.match(bare)
    if lead:
        rest = lead.group("rest")
        if re.match(r"^(?:but|however|except|although|though)\b", rest) or _CORRECTION_ANY.search(rest):
            return Intent(INTENT_CORRECTION, "medium", "correction.approval_with_but")
        if _NEW_TASK_START.match(rest) or re.match(r"^(?:now|next|then)\b", rest):
            return Intent(INTENT_NEW_TASK, "medium", "new_task.after_approval")
        if _CONTINUE_START.match(rest):
            return Intent(INTENT_CONTINUE, "medium", "continue.after_approval")
        return Intent(INTENT_APPROVAL, "medium", "approval.leading_phrase")
    if _CORRECTION_ANY.search(text):
        return Intent(INTENT_CORRECTION, "medium", "correction.phrase_in_body")
    if _REDIRECT_ANY.search(text):
        return Intent(INTENT_REDIRECT, "medium", "redirect.phrase_in_body")
    if not _REQUEST_QUESTION.match(first) and (_QUESTION_START.match(first) or bare.endswith("?") or text.endswith("?")):
        return Intent(INTENT_CLARIFICATION, "high" if _QUESTION_START.match(first) else "medium",
                      "clarification.question")
    if len(text) >= _NEW_TASK_MIN_CHARS and not _BACK_REFERENCE.search(first):
        return Intent(INTENT_NEW_TASK, "low", "new_task.long_without_reference")
    return Intent(INTENT_UNKNOWN, "low", "no_rule")


__all__ = [
    "CONFIDENCES",
    "INTENTS",
    "INTENT_APPROVAL",
    "INTENT_CLARIFICATION",
    "INTENT_CONTINUE",
    "INTENT_CORRECTION",
    "INTENT_NEW_TASK",
    "INTENT_REDIRECT",
    "INTENT_UNKNOWN",
    "METHOD",
    "Intent",
    "classify_intent",
]
