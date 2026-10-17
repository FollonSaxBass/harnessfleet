"""Tests for internal worker frame codec."""

from __future__ import annotations

import struct

import pytest

from harnessfleet.worker_frames import (
    FRAME_MAGIC,
    HEADER_SIZE,
    MAX_BODY_SIZE,
    MAX_FRAME_SIZE,
    MAX_GENERATION,
    MAX_SEQUENCE,
    MIN_GENERATION,
    MIN_SEQUENCE,
    ProtocolError,
    decode_frame,
    encode_frame,
)

EXPECTED_DIAGNOSTICS = {
    "generation must be an integer",
    "generation out of bounds",
    "sequence must be an integer",
    "sequence out of bounds",
    "body must be bytes",
    "body exceeds maximum frame capacity",
    "data must be bytes",
    "frame too short",
    "frame exceeds maximum size",
    "invalid magic",
    "body length mismatch",
    "generation mismatch",
    "sequence mismatch",
}


class PoisonObject:
    """Object that raises an assertion error if any conversion or inspect method is called."""

    def __repr__(self) -> str:
        raise AssertionError("PoisonObject.__repr__ must not be called")

    def __str__(self) -> str:
        raise AssertionError("PoisonObject.__str__ must not be called")

    def __bytes__(self) -> bytes:
        raise AssertionError("PoisonObject.__bytes__ must not be called")

    def __int__(self) -> int:
        raise AssertionError("PoisonObject.__int__ must not be called")

    def __index__(self) -> int:
        raise AssertionError("PoisonObject.__index__ must not be called")

    def __len__(self) -> int:
        raise AssertionError("PoisonObject.__len__ must not be called")

    def __iter__(self):
        raise AssertionError("PoisonObject.__iter__ must not be called")

    def __bool__(self) -> bool:
        raise AssertionError("PoisonObject.__bool__ must not be called")


class PoisonSubclassBytes(bytes):
    """Bytes subclass whose conversion/inspection methods raise AssertionError."""

    def __repr__(self) -> str:
        raise AssertionError("PoisonSubclassBytes.__repr__ must not be called")

    def __str__(self) -> str:
        raise AssertionError("PoisonSubclassBytes.__str__ must not be called")


class PoisonSubclassInt(int):
    """Int subclass whose conversion/inspection methods raise AssertionError."""

    def __repr__(self) -> str:
        raise AssertionError("PoisonSubclassInt.__repr__ must not be called")

    def __str__(self) -> str:
        raise AssertionError("PoisonSubclassInt.__str__ must not be called")


def test_protocol_error_hierarchy():
    """ProtocolError must inherit from ValueError."""
    assert issubclass(ProtocolError, ValueError)
    err = ProtocolError("test error")
    assert isinstance(err, ValueError)
    assert str(err) == "test error"


def test_keyword_only_arguments_enforced():
    """Functions must require generation and sequence as keyword-only arguments."""
    with pytest.raises(TypeError):
        encode_frame(b"", 1, 0)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        decode_frame(b"", 1, 0)  # type: ignore[call-arg]


def test_roundtrip_empty():
    """Roundtrip with an empty payload body."""
    wire = encode_frame(b"", generation=1, sequence=0)
    assert len(wire) == HEADER_SIZE
    decoded = decode_frame(wire, generation=1, sequence=0)
    assert decoded == b""


def test_roundtrip_ordinary():
    """Roundtrip with ordinary payloads of varied contents and boundaries."""
    payloads = [
        b"hello worker boundary",
        b"\x00\xff\xfe\x01\x02\x00\x00\x7f",
        b"x" * 128,
        b"boundary-test-payload" * 10,
    ]
    for idx, body in enumerate(payloads):
        wire = encode_frame(body, generation=42, sequence=idx)
        assert len(wire) == HEADER_SIZE + len(body)
        decoded = decode_frame(wire, generation=42, sequence=idx)
        assert decoded == body


def test_roundtrip_maximum():
    """Roundtrip with maximum capacity body (4076 bytes)."""
    body = b"M" * MAX_BODY_SIZE
    wire = encode_frame(body, generation=MAX_GENERATION, sequence=MAX_SEQUENCE)
    assert len(wire) == MAX_FRAME_SIZE
    decoded = decode_frame(wire, generation=MAX_GENERATION, sequence=MAX_SEQUENCE)
    assert decoded == body


def test_exact_wire_bytes_independent_struct_pack():
    """Independently construct wire bytes with struct.pack and compare with codec."""
    gen = 0x123456789ABCDEF0
    seq = 0x0FEDCBA9
    body = b"cross-checked payload"
    expected_wire = struct.pack("!4sQII", FRAME_MAGIC, gen, seq, len(body)) + body

    actual_wire = encode_frame(body, generation=gen, sequence=seq)
    assert actual_wire == expected_wire

    decoded = decode_frame(expected_wire, generation=gen, sequence=seq)
    assert decoded == body


def test_generation_boundaries():
    """Validate generation boundary values and rejections."""
    # Valid boundaries
    w_min = encode_frame(b"min", generation=MIN_GENERATION, sequence=0)
    assert decode_frame(w_min, generation=MIN_GENERATION, sequence=0) == b"min"

    w_max = encode_frame(b"max", generation=MAX_GENERATION, sequence=0)
    assert decode_frame(w_max, generation=MAX_GENERATION, sequence=0) == b"max"

    invalid_gens = [
        0,
        -1,
        -100,
        -(1 << 64),
        1 << 64,
        (1 << 64) + 1,
    ]
    for bad_gen in invalid_gens:
        with pytest.raises(ProtocolError) as exc_enc:
            encode_frame(b"payload", generation=bad_gen, sequence=0)
        assert str(exc_enc.value) == "generation out of bounds"

        with pytest.raises(ProtocolError) as exc_dec:
            decode_frame(w_min, generation=bad_gen, sequence=0)
        assert str(exc_dec.value) == "generation out of bounds"


def test_sequence_boundaries():
    """Validate sequence boundary values and rejections."""
    # Valid boundaries
    w_min = encode_frame(b"min", generation=1, sequence=MIN_SEQUENCE)
    assert decode_frame(w_min, generation=1, sequence=MIN_SEQUENCE) == b"min"

    w_max = encode_frame(b"max", generation=1, sequence=MAX_SEQUENCE)
    assert decode_frame(w_max, generation=1, sequence=MAX_SEQUENCE) == b"max"

    invalid_seqs = [
        -1,
        -100,
        -(1 << 32),
        1 << 32,
        (1 << 32) + 1,
    ]
    for bad_seq in invalid_seqs:
        with pytest.raises(ProtocolError) as exc_enc:
            encode_frame(b"payload", generation=1, sequence=bad_seq)
        assert str(exc_enc.value) == "sequence out of bounds"

        with pytest.raises(ProtocolError) as exc_dec:
            decode_frame(w_min, generation=1, sequence=bad_seq)
        assert str(exc_dec.value) == "sequence out of bounds"


def test_wrong_types_and_bools_and_nonfinite_inputs():
    """Reject bools, floats, strings, None, and structures for generation and sequence."""
    invalid_ints = [
        True,
        False,
        1.0,
        0.0,
        float("inf"),
        float("-inf"),
        float("nan"),
        "1",
        "0",
        None,
        [1],
        {"gen": 1},
    ]

    dummy_frame = encode_frame(b"", generation=1, sequence=0)

    for bad_val in invalid_ints:
        # generation checks
        with pytest.raises(ProtocolError) as exc_gen_enc:
            encode_frame(b"", generation=bad_val, sequence=0)  # type: ignore[arg-type]
        assert str(exc_gen_enc.value) == "generation must be an integer"

        with pytest.raises(ProtocolError) as exc_gen_dec:
            decode_frame(dummy_frame, generation=bad_val, sequence=0)  # type: ignore[arg-type]
        assert str(exc_gen_dec.value) == "generation must be an integer"

        # sequence checks
        with pytest.raises(ProtocolError) as exc_seq_enc:
            encode_frame(b"", generation=1, sequence=bad_val)  # type: ignore[arg-type]
        assert str(exc_seq_enc.value) == "sequence must be an integer"

        with pytest.raises(ProtocolError) as exc_seq_dec:
            decode_frame(dummy_frame, generation=1, sequence=bad_val)  # type: ignore[arg-type]
        assert str(exc_seq_dec.value) == "sequence must be an integer"


def test_wrong_types_for_body_and_data():
    """Reject non-bytes objects for body and data without conversion."""
    invalid_bytes = [
        bytearray(b"payload"),
        memoryview(b"payload"),
        "payload",
        12345,
        None,
        True,
        [b"payload"],
    ]

    for bad_body in invalid_bytes:
        with pytest.raises(ProtocolError) as exc_body:
            encode_frame(bad_body, generation=1, sequence=0)  # type: ignore[arg-type]
        assert str(exc_body.value) == "body must be bytes"

    for bad_data in invalid_bytes:
        with pytest.raises(ProtocolError) as exc_data:
            decode_frame(bad_data, generation=1, sequence=0)  # type: ignore[arg-type]
        assert str(exc_data.value) == "data must be bytes"


def test_encode_oversized_body_rejected_without_truncation():
    """Encode must reject bodies larger than 4076 bytes without truncation."""
    with pytest.raises(ProtocolError) as exc_4077:
        encode_frame(b"A" * (MAX_BODY_SIZE + 1), generation=1, sequence=0)
    assert str(exc_4077.value) == "body exceeds maximum frame capacity"

    with pytest.raises(ProtocolError) as exc_large:
        encode_frame(b"B" * (MAX_FRAME_SIZE * 2), generation=1, sequence=0)
    assert str(exc_large.value) == "body exceeds maximum frame capacity"


def test_decode_short_data():
    """Decode must reject frames shorter than the 20-byte header."""
    short_inputs = [
        b"",
        b"H",
        b"HF",
        b"HFC",
        FRAME_MAGIC,
        FRAME_MAGIC + b"\x00" * 15,  # 19 bytes
    ]
    for short_buf in short_inputs:
        with pytest.raises(ProtocolError) as exc:
            decode_frame(short_buf, generation=1, sequence=0)
        assert str(exc.value) == "frame too short"


def test_decode_oversized_frame():
    """Decode must reject frames exceeding 4096 bytes."""
    oversized = FRAME_MAGIC + b"\x00" * (MAX_FRAME_SIZE - 4 + 1)  # 4097 bytes
    with pytest.raises(ProtocolError) as exc:
        decode_frame(oversized, generation=1, sequence=0)
    assert str(exc.value) == "frame exceeds maximum size"

    huge = FRAME_MAGIC + b"\x00" * 8192
    with pytest.raises(ProtocolError) as exc_huge:
        decode_frame(huge, generation=1, sequence=0)
    assert str(exc_huge.value) == "frame exceeds maximum size"


def test_decode_bad_magic():
    """Decode must reject frames with invalid magic prefix."""
    bad_magics = [
        b"NOPE",
        b"hfcp",
        b"\x00\x00\x00\x00",
        b"HFCP"[::-1],
    ]
    for magic in bad_magics:
        data = struct.pack("!4sQII", magic, 1, 0, 0)
        with pytest.raises(ProtocolError) as exc:
            decode_frame(data, generation=1, sequence=0)
        assert str(exc.value) == "invalid magic"


def test_decode_mismatched_length_truncated_body():
    """Decode must reject frames where actual payload is shorter than declared length."""
    # Declares 10 body bytes, but only supplies 4 body bytes
    data = struct.pack("!4sQII", FRAME_MAGIC, 1, 0, 10) + b"1234"
    with pytest.raises(ProtocolError) as exc:
        decode_frame(data, generation=1, sequence=0)
    assert str(exc.value) == "body length mismatch"


def test_decode_mismatched_length_trailing_data():
    """Decode must reject frames where actual payload is longer than declared length."""
    # Declares 4 body bytes, but supplies 8 body bytes (4 trailing bytes)
    data = struct.pack("!4sQII", FRAME_MAGIC, 1, 0, 4) + b"1234extra!"
    with pytest.raises(ProtocolError) as exc:
        decode_frame(data, generation=1, sequence=0)
    assert str(exc.value) == "body length mismatch"


def test_decode_mismatched_length_oversized_declared():
    """Decode must reject frames where declared length is arbitrarily large."""
    for declared in [MAX_FRAME_SIZE + 1, 0xFFFFFFFF]:
        data = struct.pack("!4sQII", FRAME_MAGIC, 1, 0, declared)
        with pytest.raises(ProtocolError) as exc:
            decode_frame(data, generation=1, sequence=0)
        assert str(exc.value) == "body length mismatch"


def test_decode_stale_or_foreign_generation():
    """Decode must reject mismatch between wire generation and expected generation."""
    wire = encode_frame(b"ok", generation=10, sequence=5)

    with pytest.raises(ProtocolError) as exc_low:
        decode_frame(wire, generation=9, sequence=5)
    assert str(exc_low.value) == "generation mismatch"

    with pytest.raises(ProtocolError) as exc_high:
        decode_frame(wire, generation=11, sequence=5)
    assert str(exc_high.value) == "generation mismatch"


def test_decode_stale_or_foreign_sequence():
    """Decode must reject mismatch between wire sequence and expected sequence."""
    wire = encode_frame(b"ok", generation=1, sequence=100)

    with pytest.raises(ProtocolError) as exc_low:
        decode_frame(wire, generation=1, sequence=99)
    assert str(exc_low.value) == "sequence mismatch"

    with pytest.raises(ProtocolError) as exc_high:
        decode_frame(wire, generation=1, sequence=101)
    assert str(exc_high.value) == "sequence mismatch"


def test_decode_validates_caller_expectations_even_when_data_is_malformed():
    """Caller expectation validation runs before parsing malformed data."""
    malformed_payloads = [
        b"",
        b"short",
        b"invalid magic and garbage wire content",
        b"\xff" * 50,
    ]
    for malformed in malformed_payloads:
        # Invalid generation expected by caller
        with pytest.raises(ProtocolError) as exc_gen:
            decode_frame(malformed, generation=0, sequence=0)
        assert str(exc_gen.value) == "generation out of bounds"

        with pytest.raises(ProtocolError) as exc_gen_type:
            decode_frame(malformed, generation=True, sequence=0)  # type: ignore[arg-type]
        assert str(exc_gen_type.value) == "generation must be an integer"

        # Invalid sequence expected by caller
        with pytest.raises(ProtocolError) as exc_seq:
            decode_frame(malformed, generation=1, sequence=-1)
        assert str(exc_seq.value) == "sequence out of bounds"

        with pytest.raises(ProtocolError) as exc_seq_type:
            decode_frame(malformed, generation=1, sequence=False)  # type: ignore[arg-type]
        assert str(exc_seq_type.value) == "sequence must be an integer"


def test_prove_rejected_objects_repr_and_conversions_not_called():
    """Verify that rejected objects never have __repr__, __str__, or conversions called."""
    poison = PoisonObject()
    poison_bytes = PoisonSubclassBytes(b"secret")
    poison_int = PoisonSubclassInt(1)

    # PoisonObject on encode
    with pytest.raises(ProtocolError) as exc:
        encode_frame(poison, generation=1, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "body must be bytes"

    with pytest.raises(ProtocolError) as exc:
        encode_frame(b"", generation=poison, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "generation must be an integer"

    with pytest.raises(ProtocolError) as exc:
        encode_frame(b"", generation=1, sequence=poison)  # type: ignore[arg-type]
    assert str(exc.value) == "sequence must be an integer"

    # PoisonObject on decode
    with pytest.raises(ProtocolError) as exc:
        decode_frame(poison, generation=1, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "data must be bytes"

    with pytest.raises(ProtocolError) as exc:
        decode_frame(b"", generation=poison, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "generation must be an integer"

    with pytest.raises(ProtocolError) as exc:
        decode_frame(b"", generation=1, sequence=poison)  # type: ignore[arg-type]
    assert str(exc.value) == "sequence must be an integer"

    # Poison subclasses
    with pytest.raises(ProtocolError) as exc:
        encode_frame(poison_bytes, generation=1, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "body must be bytes"

    with pytest.raises(ProtocolError) as exc:
        decode_frame(poison_bytes, generation=1, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "data must be bytes"

    with pytest.raises(ProtocolError) as exc:
        encode_frame(b"", generation=poison_int, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "generation must be an integer"

    with pytest.raises(ProtocolError) as exc:
        decode_frame(b"", generation=poison_int, sequence=0)  # type: ignore[arg-type]
    assert str(exc.value) == "generation must be an integer"


def test_payload_contents_never_appear_in_error_messages():
    """Verify diagnostics are fixed bounded text and never leak payload contents or secrets."""
    secret = b"SUPER_SECRET_PAYLOAD_TOKEN_ABC123"
    secret_str = "SUPER_SECRET_PAYLOAD_TOKEN_ABC123"

    # Oversized body containing secret
    oversized_secret_body = secret + b"X" * (MAX_BODY_SIZE + 1)
    with pytest.raises(ProtocolError) as exc:
        encode_frame(oversized_secret_body, generation=1, sequence=0)
    msg = str(exc.value)
    assert secret_str not in msg
    assert repr(secret) not in msg
    assert msg in EXPECTED_DIAGNOSTICS

    # Wire frame with secret payload and bad magic
    bad_magic_frame = struct.pack("!4sQII", b"BADM", 1, 0, len(secret)) + secret
    with pytest.raises(ProtocolError) as exc:
        decode_frame(bad_magic_frame, generation=1, sequence=0)
    msg = str(exc.value)
    assert secret_str not in msg
    assert repr(secret) not in msg
    assert msg in EXPECTED_DIAGNOSTICS

    # Wire frame with secret payload and length mismatch
    mismatched_frame = struct.pack("!4sQII", FRAME_MAGIC, 1, 0, len(secret) + 10) + secret
    with pytest.raises(ProtocolError) as exc:
        decode_frame(mismatched_frame, generation=1, sequence=0)
    msg = str(exc.value)
    assert secret_str not in msg
    assert repr(secret) not in msg
    assert msg in EXPECTED_DIAGNOSTICS

    # Wire frame with secret payload and wrong generation
    gen_mismatch_frame = struct.pack("!4sQII", FRAME_MAGIC, 999, 0, len(secret)) + secret
    with pytest.raises(ProtocolError) as exc:
        decode_frame(gen_mismatch_frame, generation=1, sequence=0)
    msg = str(exc.value)
    assert secret_str not in msg
    assert repr(secret) not in msg
    assert msg in EXPECTED_DIAGNOSTICS

    # Wire frame with secret payload and wrong sequence
    seq_mismatch_frame = struct.pack("!4sQII", FRAME_MAGIC, 1, 999, len(secret)) + secret
    with pytest.raises(ProtocolError) as exc:
        decode_frame(seq_mismatch_frame, generation=1, sequence=0)
    msg = str(exc.value)
    assert secret_str not in msg
    assert repr(secret) not in msg
    assert msg in EXPECTED_DIAGNOSTICS
