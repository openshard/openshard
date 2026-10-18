"""RFC 8785 JSON Canonicalization Scheme (JCS), for digests a TypeScript verifier must reproduce.

Seal v2 (docs/receipt-seal-v2.md) digests the synced receipt and the record
field commitments over these bytes. They match ECMAScript's
``JSON.stringify`` with object keys sorted by UTF-16 code units, which is
what the Platform's ``canonicalJson`` computes, so both sides get the same
sha256 byte for byte. ``tests/fixtures/seal-v2/jcs-vectors.json`` pins it.

Only JSON values are accepted: ``dict`` with ``str`` keys, ``list``,
``str``, ``int``, ``float``, ``bool`` and ``None``. NaN, infinities, strings
with unpaired surrogates and anything else raise :class:`ValueError`,
because I-JSON (RFC 7493) has no form for them.
"""

from __future__ import annotations

import hashlib
import math

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("JCS: string contains an unpaired surrogate")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _number(value: float) -> str:
    """ECMAScript Number::toString (ECMA-262 6.1.6.1.20), as RFC 8785 section 3.2.2.3 requires."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError("JCS: NaN and infinities are not JSON")
    if value == 0:
        return "0"  # also -0
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, closest to the value: the s of the spec.
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)  # value = 0.<digits> * 10**point
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    tail = f"e+{e}" if e >= 0 else f"e-{-e}"
    return sign + digits[0] + ("." + digits[1:] if k > 1 else "") + tail


def _utf16_key(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass")


def _serialise(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, int):
        # A JSON number is an IEEE double on the TypeScript side; integers past 2**53 round there.
        out.append(str(value) if abs(value) < 2**53 else _number(float(value)))
    elif isinstance(value, float):
        out.append(_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for position, item in enumerate(value):
            if position:
                out.append(",")
            _serialise(item, out)
        out.append("]")
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JCS: object keys must be strings")
        out.append("{")
        for position, key in enumerate(sorted(value, key=_utf16_key)):
            if position:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialise(value[key], out)
        out.append("}")
    else:
        raise ValueError(f"JCS: {type(value).__name__} is not a JSON value")


def canonicalize(value: object) -> bytes:
    """The RFC 8785 canonical UTF-8 bytes of *value*. Raises ``ValueError`` for non-JSON input."""
    out: list[str] = []
    try:
        _serialise(value, out)
    except (OverflowError, RecursionError) as exc:
        raise ValueError(f"JCS: {exc}") from exc
    return "".join(out).encode("utf-8")


def sha256_hex(value: object) -> str:
    """sha256 hex of :func:`canonicalize` (*value*)."""
    return hashlib.sha256(canonicalize(value)).hexdigest()


__all__ = ["canonicalize", "sha256_hex"]
