"""Internal worker frame codec for bounded channel transport."""

from __future__ import annotations

import struct

__all__ = [
    "FRAME_MAGIC",
    "HEADER_SIZE",
    "MAX_BODY_SIZE",
    "MAX_FRAME_SIZE",
    "MAX_GENERATION",
    "MAX_SEQUENCE",
    "MIN_GENERATION",
    "MIN_SEQUENCE",
    "ProtocolError",
    "decode_frame",
    "encode_frame",
]

FRAME_MAGIC: bytes = b"HFCP"
_HEADER_STRUCT: struct.Struct = struct.Struct("!4sQII")
HEADER_SIZE: int = _HEADER_STRUCT.size  # 20 bytes
MAX_FRAME_SIZE: int = 4096
MAX_BODY_SIZE: int = MAX_FRAME_SIZE - HEADER_SIZE  # 4076 bytes

MIN_GENERATION: int = 1
MAX_GENERATION: int = (1 << 64) - 1

MIN_SEQUENCE: int = 0
MAX_SEQUENCE: int = (1 << 32) - 1


class ProtocolError(ValueError):
    """Raised when frame encoding or decoding fails protocol validation."""


def _validate_generation(generation: int) -> None:
    if type(generation) is not int:
        raise ProtocolError("generation must be an integer")
    if generation < MIN_GENERATION or generation > MAX_GENERATION:
        raise ProtocolError("generation out of bounds")


def _validate_sequence(sequence: int) -> None:
    if type(sequence) is not int:
        raise ProtocolError("sequence must be an integer")
    if sequence < MIN_SEQUENCE or sequence > MAX_SEQUENCE:
        raise ProtocolError("sequence out of bounds")


def encode_frame(body: bytes, *, generation: int, sequence: int) -> bytes:
    """Encode payload body into a bounded wire frame.

    Args:
        body: Exact bytes payload to encode (0 to 4076 bytes).
        generation: Channel generation identifier (1 to 2**64 - 1).
        sequence: Monotonic frame sequence number (0 to 2**32 - 1).

    Returns:
        Encoded wire frame bytes (20 to 4096 bytes).

    Raises:
        ProtocolError: If any argument fails validation or bounds check.
    """
    _validate_generation(generation)
    _validate_sequence(sequence)
    if type(body) is not bytes:
        raise ProtocolError("body must be bytes")
    if len(body) > MAX_BODY_SIZE:
        raise ProtocolError("body exceeds maximum frame capacity")

    return _HEADER_STRUCT.pack(FRAME_MAGIC, generation, sequence, len(body)) + body


def decode_frame(data: bytes, *, generation: int, sequence: int) -> bytes:
    """Decode and validate a wire frame, returning payload body bytes.

    Args:
        data: Exact wire frame bytes to decode (20 to 4096 bytes).
        generation: Expected channel generation identifier (1 to 2**64 - 1).
        sequence: Expected monotonic frame sequence number (0 to 2**32 - 1).

    Returns:
        Decoded body bytes.

    Raises:
        ProtocolError: If frame data or caller expectation fails validation.
    """
    _validate_generation(generation)
    _validate_sequence(sequence)
    if type(data) is not bytes:
        raise ProtocolError("data must be bytes")

    total_len = len(data)
    if total_len < HEADER_SIZE:
        raise ProtocolError("frame too short")
    if total_len > MAX_FRAME_SIZE:
        raise ProtocolError("frame exceeds maximum size")

    magic, wire_gen, wire_seq, body_len = _HEADER_STRUCT.unpack_from(data, 0)
    if magic != FRAME_MAGIC:
        raise ProtocolError("invalid magic")

    actual_body_len = total_len - HEADER_SIZE
    if body_len != actual_body_len:
        raise ProtocolError("body length mismatch")

    if wire_gen != generation:
        raise ProtocolError("generation mismatch")
    if wire_seq != sequence:
        raise ProtocolError("sequence mismatch")

    return data[HEADER_SIZE:]
