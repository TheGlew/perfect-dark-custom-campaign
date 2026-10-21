"""Rare's 1173 compression: a five-byte header (0x11 0x73, then the inflated
size as three big-endian bytes) in front of a raw deflate stream."""
import zlib

MAGIC = b"\x11\x73"


def is1173(data: bytes) -> bool:
    return data[:2] == MAGIC


def inflate(data: bytes) -> bytes:
    if not is1173(data):
        raise ValueError("not a 1173 stream")
    size = int.from_bytes(data[2:5], "big")
    out = zlib.decompressobj(-15).decompress(data[5:])
    if len(out) < size:
        raise ValueError("short inflate: %d of %d" % (len(out), size))
    return out[:size]


def inflate_prefix(data: bytes):
    """Inflate a stream that may be followed by other data. Returns (inflated, consumed)."""
    if not is1173(data):
        raise ValueError("not a 1173 stream")
    size = int.from_bytes(data[2:5], "big")
    d = zlib.decompressobj(-15)
    out = d.decompress(data[5:])
    consumed = len(data) - len(d.unused_data)
    return out[:size], consumed


def deflate(data: bytes) -> bytes:
    if len(data) >= 1 << 24:
        raise ValueError("too large for a 1173 header")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    stream = c.compress(data) + c.flush()
    return MAGIC + len(data).to_bytes(3, "big") + stream
