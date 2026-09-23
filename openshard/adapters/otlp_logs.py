"""Minimal, dependency-free reader for OpenTelemetry OTLP *log* exports.

Only what an ingestion adapter needs: turn an OTLP/HTTP logs request --
binary protobuf (``application/x-protobuf``, what Cursor's OpenTelemetry
Export sends) or OTLP/JSON (what a collector's ``file`` exporter writes,
one request object per line) -- into flat :class:`LogRecord` values.

No ``opentelemetry-proto`` / ``protobuf`` dependency: the OTLP logs schema
used here is small and stable, so the handful of fields read are decoded
straight from the protobuf wire format. Everything not listed below is
skipped by wire type, so newer OTLP fields never break decoding.

Fields read (opentelemetry-proto ``logs/v1``, ``common/v1``,
``resource/v1``; ``collector/logs/v1`` for the request):

* ``ExportLogsServiceRequest.resource_logs`` (1)
* ``ResourceLogs.resource`` (1), ``.scope_logs`` (2)
* ``Resource.attributes`` (1)
* ``ScopeLogs.log_records`` (2)
* ``LogRecord.time_unix_nano`` (1, fixed64), ``.severity_number`` (2),
  ``.severity_text`` (3), ``.body`` (5), ``.attributes`` (6),
  ``.observed_time_unix_nano`` (11, fixed64), ``.event_name`` (12)
* ``KeyValue.key`` (1), ``.value`` (2)
* ``AnyValue`` string (1), bool (2), int (3), double (4), array (5),
  kvlist (6), bytes (7 -- kept as ``None``: never stored)

Pure, never executes anything, and every malformed input raises
:class:`OtlpDecodeError` rather than returning half-decoded data.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

# Bounds: a request larger than this is refused rather than decoded.
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_DEPTH = 8
_MAX_ARRAY = 64

_WT_VARINT = 0
_WT_I64 = 1
_WT_LEN = 2
_WT_I32 = 5


class OtlpDecodeError(ValueError):
    """The bytes are not a decodable OTLP logs request."""


@dataclass
class LogRecord:
    """One flattened OTLP log record with its resource attributes."""

    resource: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    body: Any = None
    event_name: str | None = None
    time_unix_nano: int | None = None
    observed_time_unix_nano: int | None = None
    severity_number: int | None = None
    severity_text: str | None = None


# ---------------------------------------------------------------------------
# Protobuf wire decoding
# ---------------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise OtlpDecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise OtlpDecodeError("varint too long")


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)``; LEN values are ``bytes``."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        number, wire_type = key >> 3, key & 7
        if number == 0:
            raise OtlpDecodeError("field number 0")
        value: Any
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == _WT_I64:
            if pos + 8 > n:
                raise OtlpDecodeError("truncated fixed64")
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == _WT_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise OtlpDecodeError("truncated length-delimited field")
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == _WT_I32:
            if pos + 4 > n:
                raise OtlpDecodeError("truncated fixed32")
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise OtlpDecodeError(f"unsupported wire type {wire_type}")
        yield number, wire_type, value


def _utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OtlpDecodeError("invalid utf-8 string") from exc


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def _pb_any_value(buf: bytes, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise OtlpDecodeError("AnyValue nested too deeply")
    value: Any = None
    for number, wt, raw in _iter_fields(buf):
        if number == 1 and wt == _WT_LEN:
            value = _utf8(raw)
        elif number == 2 and wt == _WT_VARINT:
            value = bool(raw)
        elif number == 3 and wt == _WT_VARINT:
            value = _signed64(raw)
        elif number == 4 and wt == _WT_I64:
            value = struct.unpack("<d", raw)[0]
        elif number == 5 and wt == _WT_LEN:
            items: list[Any] = []
            for n2, wt2, raw2 in _iter_fields(raw):
                if n2 == 1 and wt2 == _WT_LEN and len(items) < _MAX_ARRAY:
                    items.append(_pb_any_value(raw2, depth + 1))
            value = items
        elif number == 6 and wt == _WT_LEN:
            kv: dict[str, Any] = {}
            for n2, wt2, raw2 in _iter_fields(raw):
                if n2 == 1 and wt2 == _WT_LEN and len(kv) < _MAX_ARRAY:
                    k, v = _pb_key_value(raw2, depth + 1)
                    if k is not None:
                        kv[k] = v
            value = kv
        elif number == 7:
            value = None  # bytes values are never kept
    return value


def _pb_key_value(buf: bytes, depth: int = 0) -> tuple[str | None, Any]:
    key: str | None = None
    value: Any = None
    for number, wt, raw in _iter_fields(buf):
        if number == 1 and wt == _WT_LEN:
            key = _utf8(raw)
        elif number == 2 and wt == _WT_LEN:
            value = _pb_any_value(raw, depth)
    return key, value


def _pb_attributes(chunks: list[bytes]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw in chunks:
        k, v = _pb_key_value(raw)
        if k is not None:
            out[k] = v
    return out


def _pb_log_record(buf: bytes, resource: dict[str, Any]) -> LogRecord:
    rec = LogRecord(resource=resource)
    attrs: list[bytes] = []
    for number, wt, raw in _iter_fields(buf):
        if number == 1 and wt == _WT_I64:
            rec.time_unix_nano = struct.unpack("<Q", raw)[0] or None
        elif number == 11 and wt == _WT_I64:
            rec.observed_time_unix_nano = struct.unpack("<Q", raw)[0] or None
        elif number == 2 and wt == _WT_VARINT:
            rec.severity_number = int(raw)
        elif number == 3 and wt == _WT_LEN:
            rec.severity_text = _utf8(raw)
        elif number == 5 and wt == _WT_LEN:
            rec.body = _pb_any_value(raw)
        elif number == 6 and wt == _WT_LEN:
            attrs.append(raw)
        elif number == 12 and wt == _WT_LEN:
            rec.event_name = _utf8(raw) or None
    rec.attributes = _pb_attributes(attrs)
    return rec


def decode_logs_protobuf(data: bytes) -> list[LogRecord]:
    """Decode a binary ``ExportLogsServiceRequest``."""
    records: list[LogRecord] = []
    for number, wt, rl in _iter_fields(data):
        if number != 1 or wt != _WT_LEN:
            continue
        resource: dict[str, Any] = {}
        scope_logs: list[bytes] = []
        for n2, wt2, raw2 in _iter_fields(rl):
            if n2 == 1 and wt2 == _WT_LEN:
                resource = _pb_attributes([r for n3, w3, r in _iter_fields(raw2) if n3 == 1 and w3 == _WT_LEN])
            elif n2 == 2 and wt2 == _WT_LEN:
                scope_logs.append(raw2)
        for sl in scope_logs:
            for n3, wt3, raw3 in _iter_fields(sl):
                if n3 == 2 and wt3 == _WT_LEN:
                    records.append(_pb_log_record(raw3, resource))
    return records


# ---------------------------------------------------------------------------
# OTLP/JSON decoding (collector file exporter, hand-written fixtures)
# ---------------------------------------------------------------------------


def _get(obj: Mapping[str, Any], camel: str, snake: str) -> Any:
    return obj.get(camel, obj.get(snake))


def _json_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _json_any_value(obj: Any, depth: int = 0) -> Any:
    if not isinstance(obj, Mapping) or depth > _MAX_DEPTH:
        return None
    if "stringValue" in obj or "string_value" in obj:
        v = _get(obj, "stringValue", "string_value")
        return v if isinstance(v, str) else None
    if "boolValue" in obj or "bool_value" in obj:
        v = _get(obj, "boolValue", "bool_value")
        return v if isinstance(v, bool) else None
    if "intValue" in obj or "int_value" in obj:
        return _json_int(_get(obj, "intValue", "int_value"))
    if "doubleValue" in obj or "double_value" in obj:
        v = _get(obj, "doubleValue", "double_value")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if "arrayValue" in obj or "array_value" in obj:
        arr = _get(obj, "arrayValue", "array_value")
        values = arr.get("values") if isinstance(arr, Mapping) else None
        return [_json_any_value(v, depth + 1) for v in (values or [])[:_MAX_ARRAY]] if isinstance(values, list) else []
    if "kvlistValue" in obj or "kvlist_value" in obj:
        kvl = _get(obj, "kvlistValue", "kvlist_value")
        values = kvl.get("values") if isinstance(kvl, Mapping) else None
        return _json_attributes(values, depth + 1)
    return None


def _json_attributes(items: Any, depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(items, list):
        return out
    for kv in items:
        if isinstance(kv, Mapping) and isinstance(kv.get("key"), str):
            out[kv["key"]] = _json_any_value(kv.get("value"), depth)
    return out


def decode_logs_json(doc: Any) -> list[LogRecord]:
    """Decode one OTLP/JSON ``ExportLogsServiceRequest`` object."""
    if not isinstance(doc, Mapping):
        raise OtlpDecodeError("OTLP/JSON request must be an object")
    resource_logs = _get(doc, "resourceLogs", "resource_logs")
    if not isinstance(resource_logs, list):
        raise OtlpDecodeError("OTLP/JSON request has no resourceLogs")
    records: list[LogRecord] = []
    for rl in resource_logs:
        if not isinstance(rl, Mapping):
            continue
        res = rl.get("resource")
        resource = _json_attributes(res.get("attributes")) if isinstance(res, Mapping) else {}
        for sl in _get(rl, "scopeLogs", "scope_logs") or []:
            if not isinstance(sl, Mapping):
                continue
            for lr in _get(sl, "logRecords", "log_records") or []:
                if not isinstance(lr, Mapping):
                    continue
                event_name = _get(lr, "eventName", "event_name")
                severity_text = _get(lr, "severityText", "severity_text")
                records.append(LogRecord(
                    resource=resource,
                    attributes=_json_attributes(lr.get("attributes")),
                    body=_json_any_value(lr.get("body")),
                    event_name=event_name if isinstance(event_name, str) and event_name else None,
                    time_unix_nano=_json_int(_get(lr, "timeUnixNano", "time_unix_nano")) or None,
                    observed_time_unix_nano=_json_int(_get(lr, "observedTimeUnixNano", "observed_time_unix_nano")) or None,
                    severity_number=_json_int(_get(lr, "severityNumber", "severity_number")),
                    severity_text=severity_text if isinstance(severity_text, str) else None,
                ))
    return records


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _maybe_gunzip(data: bytes) -> bytes:
    if data[:2] != b"\x1f\x8b":
        return data
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
            out = fh.read(MAX_DECOMPRESSED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise OtlpDecodeError("invalid gzip body") from exc
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise OtlpDecodeError("decompressed body too large")
    return out


def decode_logs(data: bytes, content_type: str | None = None) -> list[LogRecord]:
    """Decode an OTLP logs payload: protobuf, a JSON object, or JSON lines.

    *content_type* selects the format when given (``json`` anywhere in it
    means JSON); otherwise it is sniffed: a body starting with ``{`` is
    JSON, anything else protobuf. Gzip bodies are decompressed first.
    """
    if len(data) > MAX_REQUEST_BYTES:
        raise OtlpDecodeError("request too large")
    data = _maybe_gunzip(data)
    ctype = (content_type or "").lower()
    is_json = "json" in ctype if ctype else data.lstrip()[:1] == b"{"
    if not is_json:
        return decode_logs_protobuf(data)
    text = data.decode("utf-8", errors="strict") if data else ""
    try:
        return decode_logs_json(json.loads(text))
    except json.JSONDecodeError:
        pass
    # JSON lines: the collector file exporter writes one request per line.
    records: list[LogRecord] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.extend(decode_logs_json(json.loads(line)))
        except json.JSONDecodeError as exc:
            raise OtlpDecodeError("invalid OTLP/JSON line") from exc
    return records


# ---------------------------------------------------------------------------
# Encoding (tests and local fixtures only)
# ---------------------------------------------------------------------------


def _pb_key(number: int, wire_type: int) -> bytes:
    return _pb_varint((number << 3) | wire_type)


def _pb_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_len(number: int, payload: bytes) -> bytes:
    return _pb_key(number, _WT_LEN) + _pb_varint(len(payload)) + payload


def _pb_encode_any(value: Any) -> bytes:
    if isinstance(value, bool):
        return _pb_key(2, _WT_VARINT) + _pb_varint(int(value))
    if isinstance(value, int):
        return _pb_key(3, _WT_VARINT) + _pb_varint(value)
    if isinstance(value, float):
        return _pb_key(4, _WT_I64) + struct.pack("<d", value)
    if isinstance(value, (list, tuple)):
        return _pb_len(5, b"".join(_pb_len(1, _pb_encode_any(v)) for v in value))
    if isinstance(value, Mapping):
        return _pb_len(6, b"".join(_pb_len(1, _pb_encode_kv(k, v)) for k, v in value.items()))
    return _pb_len(1, str(value).encode("utf-8"))


def _pb_encode_kv(key: str, value: Any) -> bytes:
    return _pb_len(1, key.encode("utf-8")) + _pb_len(2, _pb_encode_any(value))


def encode_logs_protobuf(records: list[LogRecord]) -> bytes:
    """Encode records as a binary ``ExportLogsServiceRequest`` (one ResourceLogs per record).

    For tests and local fixtures: lets the protobuf path be exercised
    without a Cursor tenant or an OpenTelemetry SDK.
    """
    out = bytearray()
    for rec in records:
        lr = bytearray()
        if rec.time_unix_nano:
            lr += _pb_key(1, _WT_I64) + struct.pack("<Q", rec.time_unix_nano)
        if rec.severity_number is not None:
            lr += _pb_key(2, _WT_VARINT) + _pb_varint(rec.severity_number)
        if rec.severity_text:
            lr += _pb_len(3, rec.severity_text.encode("utf-8"))
        if rec.body is not None:
            lr += _pb_len(5, _pb_encode_any(rec.body))
        for k, v in rec.attributes.items():
            lr += _pb_len(6, _pb_encode_kv(k, v))
        if rec.observed_time_unix_nano:
            lr += _pb_key(11, _WT_I64) + struct.pack("<Q", rec.observed_time_unix_nano)
        if rec.event_name:
            lr += _pb_len(12, rec.event_name.encode("utf-8"))
        resource = b"".join(_pb_len(1, _pb_encode_kv(k, v)) for k, v in rec.resource.items())
        scope_logs = _pb_len(2, bytes(lr))
        out += _pb_len(1, _pb_len(1, resource) + _pb_len(2, scope_logs))
    return bytes(out)
