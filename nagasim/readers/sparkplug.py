"""Sparkplug B payloads, scalar metrics only (PHASE7 7.8, D222).

A Sparkplug B payload is a protocol-buffers message. This module reads the wire format by
hand (three field shapes: varint, fixed 32 or 64 bits, length-delimited) instead of
importing `protobuf` and a generated class: what arrives is untrusted, and the whole
reader is short enough to bound and to fuzz. Nothing here writes a payload: there is no
encoder, so no command (a rebirth request included) can be built.

    Payload: 1 timestamp (ms)   2 metrics, repeated   3 seq (0..255)
    Metric:  1 name   2 alias   3 timestamp (ms)   4 datatype   7 is_null
             10 int_value  11 long_value  12 float_value  13 double_value
             14 boolean_value  15 string_value
             16 bytes, 17 dataset, 18 template, 19 extension: not read (the metric is
             returned with `skipped` set and no value)

Signed integers travel as unsigned ones and are read at their datatype's width (1 to 4: Int8,
Int16, Int32, Int64), so a sign-extended and a narrow two's complement read the same.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from nagasim.sources import ReadError

__all__ = ["Metric", "decode", "signed", "MAX_METRICS"]

MAX_METRICS = 10_000        # per payload; more is a malformed payload
_WIDTH = {1: 8, 2: 16, 3: 32, 4: 64}      # Int8, Int16, Int32, Int64: bits


@dataclass(frozen=True)
class Metric:
    name: str | None
    alias: int | None
    timestamp: int | None       # ms since the epoch, as the node stamped it
    datatype: int | None
    value: object               # None, bool, int, float or str
    skipped: bool = False       # a value kind this reader does not read


def _bad(what: str) -> ReadError:
    return ReadError("E_DATA_READ", f"malformed Sparkplug B payload ({what})",
                     "the payload option must match what the node publishes")


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise _bad("a number runs past the end")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value & 0xFFFF_FFFF_FFFF_FFFF, pos
        shift += 7


def _fields(data: bytes):
    """(field number, wire type, value) of one message, in order. A value is an int, or
    the bytes of a length-delimited field; every step consumes at least one byte."""
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > len(data):
                raise _bad("a fixed-size field runs past the end")
            value, pos = data[pos:pos + size], pos + size
        elif wire == 2:
            size, pos = _varint(data, pos)
            if size > len(data) - pos:
                raise _bad("a field is longer than the payload")
            value, pos = data[pos:pos + size], pos + size
        else:
            raise _bad(f"wire type {wire}")
        yield number, wire, value


def signed(value, datatype: int | None):
    """*value* as the signed number its datatype says it is, read at the datatype's own width
    (as Tahu's decoders do): Tahu's Python edge library sends a negative Int8 as value + 2**8,
    its Java one sign-extends it to 32 bits, and both are -1 for 255 (#442)."""
    bits = _WIDTH.get(datatype)
    if bits is None or isinstance(value, bool) or not isinstance(value, int):
        return value
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _metric(data: bytes) -> Metric:
    name = alias = timestamp = datatype = value = None
    null = skipped = False
    for number, wire, field in _fields(data):
        if number == 1 and wire == 2:
            name = field.decode("utf-8", "replace")
        elif number == 2 and wire == 0:
            alias = field
        elif number == 3 and wire == 0:
            timestamp = field
        elif number == 4 and wire == 0:
            datatype = field
        elif number == 7 and wire == 0:
            null = bool(field)
        elif number in (10, 11) and wire == 0:
            value = field
        elif number == 12 and wire == 5:
            value = struct.unpack("<f", field)[0]
        elif number == 13 and wire == 1:
            value = struct.unpack("<d", field)[0]
        elif number == 14 and wire == 0:
            value = bool(field)
        elif number == 15 and wire == 2:
            value = field.decode("utf-8", "replace")
        elif number in (16, 17, 18, 19):
            skipped = True
        # Any other field (metadata, properties, one a later version adds) is passed over.
    if null or skipped:
        value = None
    return Metric(name, alias, timestamp, datatype, signed(value, datatype), skipped)


def decode(payload: bytes) -> tuple[int | None, int | None, list[Metric]]:
    """(timestamp in ms, seq, metrics) of one Sparkplug B payload, or `ReadError`."""
    timestamp = seq = None
    metrics: list[Metric] = []
    for number, wire, field in _fields(payload):
        if number == 1 and wire == 0:
            timestamp = field
        elif number == 3 and wire == 0:
            seq = field
        elif number == 2 and wire == 2:
            if len(metrics) >= MAX_METRICS:
                raise _bad(f"more than {MAX_METRICS} metrics")
            metrics.append(_metric(field))
    return timestamp, seq, metrics
